#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import stat
import zipfile


VERSION = "0.7.0-beta"
ROOT = Path(__file__).resolve().parents[1]
USER_ITEMS = [".agents", "plugins", "src", "docs", "install.sh", "README.md", "GETTING_STARTED.md", "CHANGELOG.md", "LICENSE"]
SOURCE_EXCLUDES = {"__pycache__", ".git", ".DS_Store", ".venv", "dist", "build"}
BANNED_PARTS = {"__pycache__", ".git", ".venv", "venv", "logs"}
BANNED_SUFFIXES = {".pyc", ".pyo"}


def copy_clean(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def zip_tree(tree: Path, destination: Path, prefix: str) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(str(Path(prefix) / path.relative_to(tree)))
                info.date_time = (2026, 1, 1, 0, 0, 0)
                mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def validate(tree: Path) -> None:
    for path in tree.rglob("*"):
        relative = path.relative_to(tree)
        if any(part in BANNED_PARTS for part in relative.parts) or path.suffix in BANNED_SUFFIXES or path.name == ".DS_Store":
            raise RuntimeError(f"release contamination: {relative}")
        if path.is_file() and path.suffix in {".py", ".md", ".json", ".toml", ".sh", ""}:
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in ("p." + "erenburg", "Beyond" + "ness", "ASTRA ROTATION " + "TEST", "NEXT_" + "REASONING", "dispatcher-" + "test"):
                if token in text:
                    raise RuntimeError(f"release contains {token!r}: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    release_tree = output / f"codex-autopilot-{VERSION}"
    if release_tree.exists(): shutil.rmtree(release_tree)
    release_tree.mkdir(parents=True)
    for item in USER_ITEMS: copy_clean(ROOT / item, release_tree / item)
    validate(release_tree)
    user_zip = output / f"codex-autopilot-{VERSION}-macos.zip"
    source_stage = output / f".codex-autopilot-{VERSION}-source-stage"
    if source_stage.exists(): shutil.rmtree(source_stage)
    shutil.copytree(ROOT, source_stage, ignore=shutil.ignore_patterns(*SOURCE_EXCLUDES, "*.pyc", "*.zip"))
    validate(source_stage)
    source_zip = output / f"codex-autopilot-{VERSION}-source.zip"
    zip_tree(release_tree, user_zip, release_tree.name)
    zip_tree(source_stage, source_zip, f"codex-autopilot-{VERSION}-source")
    shutil.rmtree(source_stage)
    for path in (user_zip, source_zip):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        print(f"{digest}  {path}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
