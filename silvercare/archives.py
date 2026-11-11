import io
import logging
import re
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_MEMBER = 16 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


@dataclass
class Budget:
    used: int = 0

    def consume(self, size: int) -> None:
        self.used += size
        if size > MAX_MEMBER or self.used > MAX_EXPANDED:
            raise ValueError("Archive exceeds the per-file or total expansion safety limit")


def _zip_files(data: bytes, budget: Budget) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            budget.consume(item.file_size)
            yield item.filename, archive.read(item)


def _files(path: Path, budget: Budget) -> Iterator[tuple[str, bytes]]:
    if path.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    if path.is_dir():
        for file in sorted(path.rglob("*")):
            if file.is_file() or file.is_symlink():
                yield from _files(file, budget)
        return
    if path.suffix.lower() == ".zip":
        budget.consume(path.stat().st_size)
        yield from _zip_files(path.read_bytes(), budget)
        return
    if path.suffix.lower() not in {".tar", ".gz", ".tgz"}:
        budget.consume(path.stat().st_size)
        yield path.name, path.read_bytes()
        return
    parts: dict[str, dict[int, bytes]] = {}
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if member.issym() or member.islnk():
                raise ValueError("Archive links are not allowed")
            if not member.isfile():
                continue
            budget.consume(member.size)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Unreadable archive member")
            data = stream.read(MAX_MEMBER + 1)
            match = re.fullmatch(r"(.+\.zip)\.part(\d+)", member.name)
            if match:
                name, index = match.group(1), int(match.group(2))
                group = parts.setdefault(name, {})
                if index in group:
                    if group[index] != data:
                        raise ValueError("Conflicting duplicate archive part")
                    logger.warning("Ignoring verified identical duplicate ZIP part %s", index)
                else:
                    group[index] = data
            elif member.name.lower().endswith(".zip"):
                yield from _zip_files(data, budget)
            else:
                yield member.name, data
    for group in parts.values():
        if sorted(group) != list(range(len(group))):
            raise ValueError("Missing or noncontiguous ZIP parts")
        yield from _zip_files(b"".join(group[index] for index in range(len(group))), budget)


def source_files(path: Path) -> Iterator[tuple[str, bytes]]:
    yield from _files(path, Budget())
