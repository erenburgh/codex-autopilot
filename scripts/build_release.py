#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _package_version() -> str:
    """The version comes from the package, not from a third hard-coded copy.

    This read "0.8.0-beta" while the package was at 0.8.2: the release
    build script would have named the archive two versions back. The third
    case of the same disease in one day - before it the memory MCP server
    version and the client version in the App Server handshake diverged.
    """

    source = (ROOT / "src" / "codex_autopilot" / "__init__.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("__version__ not found in the package")


VERSION = _package_version()
# The tests travel to the user with the sources: the installer places them
# next to the runtime, and the on-call engineer proves a repair with them.
# An archive without them did not install at all - install.sh failed
# copying tests.
USER_ITEMS = [".agents", ".gitignore", "plugins", "src", "tests", "scripts", "build_backend", "pyproject.toml", "docs", "install.sh", "README.md", "GETTING_STARTED.md", "CHANGELOG.md", "LICENSE"]
# Working notes that stay in the repository and are not part of what the
# user receives. The list is a build input, not a description of anything.
INTERNAL_DOCS = {
    "docs/V1_TARGET.md",
    "docs/V1_RUN.md",
    "docs/RELEASE_VERIFICATION_0.9.0-beta.md",
    "docs/M11_COMPLETION.md",
    "docs/M11_CONTRACT_CHECKPOINT.md",
    "docs/RELEASE_REPORT_0.8.0-beta.md",
}
# .codex-autopilot is the run state in THIS repository: plan, journal,
# project memory, logs. It belongs to whoever worked here and must never
# get into the source archive. The release guard caught it by absolute
# paths, but the consequence is not what to catch. patches is the
# directory of applied runtime patches: the state of the machine that
# repaired, not source.
# The live-acceptance harness carries the only flags in this repository
# that can answer an approval. docs/SECURITY.md tells the reader it is not
# in the user archive; it was, which made a security document say something
# untrue about what the reader had just installed.
DEV_ONLY = {"scripts/live_acceptance.py"}

SOURCE_EXCLUDES = {"__pycache__", ".git", ".DS_Store", ".venv", "dist", "build", ".codex-autopilot", "patches"}
BANNED_PARTS = {"__pycache__", ".git", ".venv", "venv", "logs"}
BANNED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".wal", ".shm"}

# Files Autopilot generates in every project by itself. In the skill
# archive they are the leftovers of someone else's run.
GENERATED_FILES = {"ROADMAP.md", "PROJECT_STATE.md", "DECISIONS.md", "HANDOFF.md"}


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
            # The game's name also travels in lower case - as a run name, a
            # fixture helper, a path - and the capitalised token alone let a
            # whole release series carry it into comments, tests and docs.
            for token in ("p." + "erenburg", "Beyond" + "ness", "beyond" + "ness", "ASTRA ROTATION " + "TEST", "NEXT_" + "REASONING", "dispatcher-" + "test"):
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
    for internal in INTERNAL_DOCS | DEV_ONLY:
        (release_tree / internal).unlink(missing_ok=True)
    validate(release_tree)
    user_zip = output / f"codex-autopilot-{VERSION}-macos.zip"
    source_stage = output / f".codex-autopilot-{VERSION}-source-stage"
    if source_stage.exists(): shutil.rmtree(source_stage)
    shutil.copytree(ROOT, source_stage, ignore=shutil.ignore_patterns(*SOURCE_EXCLUDES, "*.pyc", "*.zip"))
    # The source archive is the repository for whoever wants to build or
    # extend. The internal documents belong to neither, and the source ZIP
    # hangs in the same public release as the user one: exclude from both.
    for internal in INTERNAL_DOCS | GENERATED_FILES:
        (source_stage / internal).unlink(missing_ok=True)
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
