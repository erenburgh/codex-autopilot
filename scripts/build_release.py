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
    """Версия берётся из пакета, а не из третьей прибитой копии.

    Здесь стояло "0.8.0-beta", когда пакет был на 0.8.2: скрипт сборки
    релиза назвал бы архив двумя версиями назад. Это третий случай той
    же болезни за день - до него разошлись версия MCP-сервера памяти и
    версия клиента в рукопожатии App Server.
    """

    source = (ROOT / "src" / "codex_autopilot" / "__init__.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("не нашла __version__ в пакете")


VERSION = _package_version()
USER_ITEMS = [".agents", "plugins", "src", "docs", "install.sh", "README.md", "GETTING_STARTED.md", "CHANGELOG.md", "LICENSE"]
# Внутренние документы, которые живут в репозитории ради воркеров, но не
# уезжают пользователю: целевая спецификация следующей версии - это
# рабочий план и коммерческое позиционирование, а не документация продукта.
INTERNAL_DOCS = {
    # Целевая спецификация следующей версии: рабочий план и коммерческое
    # позиционирование, а не документация продукта.
    "docs/V1_TARGET.md",
    # Записи о том, как строился сам скилл. Пользователю они не нужны:
    # это аудит наших собственных прогонов и отчёты о починке вех.
    "docs/RELEASE_VERIFICATION_0.9.0-beta.md",
    "docs/M11_COMPLETION.md",
    "docs/M11_CONTRACT_CHECKPOINT.md",
    "docs/RELEASE_REPORT_0.8.0-beta.md",
}
# .codex-autopilot - состояние прогона в ЭТОМ репозитории: план, журнал,
# память проекта, логи. Оно принадлежит тому, кто здесь работал, и в
# исходный архив попадать не должно ни при каких условиях. Защита
# релиза ловила его по абсолютным путям, но ловить надо не следствие.
SOURCE_EXCLUDES = {"__pycache__", ".git", ".DS_Store", ".venv", "dist", "build", ".codex-autopilot"}
BANNED_PARTS = {"__pycache__", ".git", ".venv", "venv", "logs"}
BANNED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".wal", ".shm"}

# Файлы, которые автопилот генерирует в каждом проекте сам. В архиве
# скилла это остаток чужого прогона.
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
    for internal in INTERNAL_DOCS:
        (release_tree / internal).unlink(missing_ok=True)
    validate(release_tree)
    user_zip = output / f"codex-autopilot-{VERSION}-macos.zip"
    source_stage = output / f".codex-autopilot-{VERSION}-source-stage"
    if source_stage.exists(): shutil.rmtree(source_stage)
    shutil.copytree(ROOT, source_stage, ignore=shutil.ignore_patterns(*SOURCE_EXCLUDES, "*.pyc", "*.zip"))
    # Исходный архив - это репозиторий для того, кто хочет собрать или
    # доработать. Внутренние документы не относятся ни к тому, ни к
    # другому, а исходный ZIP висит в том же публичном релизе, что и
    # пользовательский: исключать надо из обоих.
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
