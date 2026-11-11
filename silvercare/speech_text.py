import re

SOURCE_HEADING = re.compile(
    r"^(?:출처|참고\s*(?:자료|문헌)|sources?|references?)"
    r"(?:\s*[:：]\s*.*|\s*)$",
    re.IGNORECASE,
)
CITATION = re.compile(r"\[\s*\d+(?:\s*(?:[,，–-]|및)\s*\d+)*\s*\]")
SOURCE_ENTRY = re.compile(
    r"^(?:\[\d+\]|\d+[.)]\s|[-*+•]\s|https?://|\[[^\]]+\]\(https?://)",
    re.IGNORECASE,
)


def spoken_text(text: str) -> str:
    lines: list[str] = []
    in_sources = False
    source_break = False
    for line in text.splitlines():
        stripped = line.strip()
        heading = re.sub(r"^#{1,6}\s+", "", stripped)
        heading = heading.replace("*", "").replace("_", "")
        if SOURCE_HEADING.fullmatch(heading):
            in_sources = True
            source_break = False
            continue
        if in_sources:
            if not stripped:
                source_break = True
                continue
            entry = re.sub(r"^(?:[-*+•]|\d+[.)])\s+", "", stripped)
            explicit_reference = bool(re.match(r"\[\d+\]|https?://|\[.+\]\(https?://", entry))
            if explicit_reference or (not source_break and SOURCE_ENTRY.match(stripped)):
                continue
            in_sources = False
        lines.append(line)
    result = "\n".join(lines)
    result = re.sub(r"!\[[^\]]*\]\([^)\n]*\)", "", result)
    result = re.sub(r"\[([^\]\n]+)\]\((?:[^()\n]|\([^()\n]*\))*\)", r"\1", result)
    result = re.sub(r"(?m)^[ \t]*\[\d+\]:[ \t]*\S+.*$", "", result)
    result = CITATION.sub("", result)
    result = re.sub(r"https?://[^\s<>가-힣]+", "원문 링크", result)
    result = re.sub(r"원문 링크\s+(에서|을|를|로|에)(?=\s|[가-힣])", r"원문 링크\1", result)
    result = re.sub(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*|[-*+•][ \t]+|\d+[.)][ \t]+)", "", result)
    result = re.sub(r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$", "", result)
    result = re.sub(r"(?m)^[ \t]*(?:[-*_][ \t]*){3,}$", "", result)
    result = re.sub(r"~~([^~\n]+)~~", "", result)
    result = result.replace("**", "").replace("__", "").replace("`", "")
    result = re.sub(r"(?<!\w)\*([^*\n]+)\*", r"\1", result)
    result = re.sub(r"(?<!\w)_([^_\n]+)_", r"\1", result)
    result = re.sub(r"[ \t]+([.,!?。，！？])", r"\1", result)
    result = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in result.splitlines())
    return re.sub(r"\n{3,}", "\n\n", result).strip()
