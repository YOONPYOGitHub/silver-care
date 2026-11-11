import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required to verify ignore rules")
def test_gitignore_keeps_source_but_excludes_private_generated_and_provider_files(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    excluded = [
        ".env",
        ".env.production",
        "production.env",
        ".envrc",
        ".direnv/vars",
        ".ssh/id_ed25519",
        "id_rsa",
        "nested/.env.local",
        ".runtime/deployment.json",
        "data/raw/source.json",
        "data/processed/cases.jsonl",
        ".azure/token-cache.json",
        ".vscode/settings.json",
        "secret.pem",
        "secret.key",
        "token.kubeconfig",
        "docs/openApi(guide).hwp",
        "docs/nested-guide.docx",
        "docs/manual.hwpx",
        "source.tar",
        "voice.wav",
        "cache.sqlite3",
        "cache.sqlite3-wal",
        "output.jsonl.partial",
        "debug.log",
        "build/lib/module.py",
        "__pycache__/module.pyc",
        ".pytest_cache/results",
        ".DS_Store",
    ]
    included = [
        ".env.example",
        ".gitignore",
        ".dockerignore",
        "uv.lock",
        "pyproject.toml",
        "README.md",
        "docs/rag.md",
        "silvercare/static/mark.svg",
        "silvercare/app.py",
        "tests/test_app.py",
        "infra/main.bicep",
        "data/catalog.json",
        "data/reference.json",
        "data/manifests/source.json",
        "data/audience-evaluation.json",
    ]
    for relative in excluded + included:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "--no-index", "--stdin"],
        input="\n".join(excluded + included) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert set(result.stdout.splitlines()) == set(excluded)
