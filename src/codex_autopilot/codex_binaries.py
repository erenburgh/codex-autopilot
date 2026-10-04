"""Which Codex a run talks to, and why more than one exists.

Codex Desktop carries its own `codex` inside the application bundle and
updates it with itself. A separately installed CLI - Homebrew, npm - is a
different file on a different schedule. They are not interchangeable:
they serve different model catalogs.

Measured on 22-23 Sep 2026. Desktop offered a newly released model in its
own picker while `model/list` through the CLI on PATH did not list it at
all, because that CLI had stayed at 0.154 while Desktop's was 0.155. A
project pinned to that model then refused to start - correctly, and for a
reason that looked like "your account does not have this model" when the
truth was "the client you are talking through cannot see it".

So the channel is chosen explicitly and written into the project's config
where it can be read and changed, rather than resolved from PATH by
accident. Nothing here switches a channel behind anyone's back: it picks a
default at project creation, and it tells a refusal what else was on the
machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


# Codex Desktop on macOS, shipped as ChatGPT.app. Read at call time, never
# captured, so a test points it at a fake bundle.
DESKTOP_APP = Path("/Applications/ChatGPT.app")

# Where the app keeps its Codex, relative to the bundle.
#
# Until the ChatGPT update of 3 Oct 2026 it was one executable,
# Contents/Resources/codex, and this module named that path outright. The
# update replaced it with a package directory described by a manifest -
# measured on 4 Oct 2026, Contents/Resources/codex-cli/codex-package.json:
#
#   {"layoutVersion": 1, "version": "0.160.0", "entrypoint": "bin/codex", ...}
#
# - and the old path stopped existing. From that day a machine without a
# separate CLI found no Codex at all, and a machine with an older Homebrew
# CLI picked that one, which did not serve the pinned model. So the package
# is read through its own manifest, the entrypoint being whatever it names,
# and the old file is still a candidate in case an older app is installed.
LEGACY_EXECUTABLE = Path("Contents/Resources/codex")
PACKAGE_DIR = Path("Contents/Resources/codex-cli")
PACKAGE_MANIFEST = "codex-package.json"
# Every address of the app's Codex that a project config can carry from
# this runtime: bootstrap wrote the legacy one from 0.12.2 to 0.14.0, and
# writes the layout-1 entrypoint from 0.14.1. When the app moves its Codex
# again, a config holding either still names the same app.
KNOWN_ADDRESSES = (LEGACY_EXECUTABLE, PACKAGE_DIR / "bin" / "codex")


def _bundle(app: Path | None) -> Path:
    return DESKTOP_APP if app is None else Path(app)


def _packaged_entrypoint(app: Path) -> Path | None:
    """The executable the app's Codex package names, or None.

    A manifest that is missing, unreadable, not JSON, has no entrypoint, or
    names something outside its own directory is not a candidate - and not
    a reason to raise either: discovery only picks a default and explains a
    refusal. The entrypoint is relative to the manifest's directory; an
    absolute one, or one that climbs out with `..` or a symlink, would let
    a manifest point the run at any executable on the machine.
    """

    package = app / PACKAGE_DIR
    try:
        manifest = json.loads((package / PACKAGE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    entry = manifest.get("entrypoint")
    if not isinstance(entry, str):
        return None
    candidate = Path(os.path.normpath(package / entry))
    try:
        candidate.resolve().relative_to(package.resolve())
        present = candidate.is_file()
    except (OSError, ValueError, RuntimeError):
        return None
    return candidate if present else None


def desktop_codex_candidates(app: Path | None = None) -> tuple[Path, ...]:
    """The app's Codex at each address where it is present, current first."""

    bundle = _bundle(app)
    found: list[Path] = []
    entry = _packaged_entrypoint(bundle)
    if entry is not None:
        found.append(entry)
    legacy = bundle / LEGACY_EXECUTABLE
    if legacy.is_file():
        found.append(legacy)
    return tuple(found)


def desktop_codex(app: Path | None = None) -> Path | None:
    """The Codex the app carries now, or None when it carries none."""

    found = desktop_codex_candidates(app)
    return found[0] if found else None


def relocated_desktop_codex(configured: str, app: Path | None = None) -> Path | None:
    """The app's current Codex, when the configured one is its old address.

    Measured 4 Oct 2026: projects created before ChatGPT's update of 3 Oct
    carry binary = ".../ChatGPT.app/Contents/Resources/codex". That file is
    gone, App Server could not be started (ENOENT), and the hook-trust gate
    refused with "hooks/list: [Errno 2] No such file or directory" - no run
    could start. The same app has the same Codex at a new address, and
    following it changes nothing about what the project is served.

    Only that. A configured binary missing anywhere else - a removed CLI, a
    typo - is not replaced: another Codex is another channel with another
    model catalog, and choosing it stays the owner's decision. The config is
    not rewritten either; the caller says what it used instead.
    """

    bundle = _bundle(app)
    try:
        raw = os.path.expanduser(str(configured))
        if not os.path.isabs(raw):
            return None
        recorded = os.path.realpath(raw)
        known = {os.path.realpath(bundle / address) for address in KNOWN_ADDRESSES}
        if recorded not in known or os.path.isfile(recorded):
            return None
    except (OSError, ValueError):
        return None
    return desktop_codex(bundle)


def relocation_note(configured: str, current: str, *, russian: bool = False) -> str:
    """One line: what the config names, what is used, how to pin it."""

    pinned = f"binary = {json.dumps(str(current), ensure_ascii=False)}"
    if russian:
        return (
            f"{configured} больше нет — ChatGPT перенёс встроенный Codex, "
            f"используется {current} из того же приложения. Закрепить: строка "
            f"{pinned} в .codex-autopilot/config.toml."
        )
    return (
        f"{configured} no longer exists — ChatGPT moved its bundled Codex, "
        f"using {current} from the same app. To pin it, change the line to "
        f"{pinned} in .codex-autopilot/config.toml."
    )


def _version(path: str) -> tuple[int, ...]:
    """The version a binary reports, or () when it will not say.

    A binary that cannot be asked sorts lowest rather than raising: this
    picks a default and explains a refusal, and neither is worth stopping
    a run over.
    """

    try:
        done = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    text = f"{done.stdout} {done.stderr}"
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not found:
        return ()
    return tuple(int(part) for part in found.groups())


def discover_codex_binaries() -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Every Codex on this machine we know how to find, newest last."""

    candidates: list[str] = []
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(on_path)
    candidates.extend(str(path) for path in desktop_codex_candidates())
    seen: dict[str, tuple[int, ...]] = {}
    for candidate in candidates:
        resolved = str(Path(candidate).resolve(strict=False))
        if resolved in seen:
            continue
        seen[resolved] = _version(candidate)
    return tuple(sorted(((path, ver) for path, ver in seen.items()), key=lambda item: item[1]))


def newest_codex_binary(default: str = "codex") -> str:
    """The newest Codex to start a project with, or the plain name.

    Written into the project's config at creation so the choice is visible
    and editable, never re-derived silently on a later run.
    """

    found = discover_codex_binaries()
    if not found:
        return default
    path, version = found[-1]
    if not version:
        return default
    return path


def binaries_serving(model_id: str) -> tuple[str, ...]:
    """Which Codex binaries on this machine list this model.

    Used only to explain a refusal: a run that cannot see its pinned model
    should be told whether another client on the same machine can, instead
    of leaving the reader to conclude their account lost the model.
    """

    from .appserver import AppServerClient
    import tempfile

    serving: list[str] = []
    for path, _version in discover_codex_binaries():
        try:
            with tempfile.NamedTemporaryFile(suffix=".jsonl") as handle:
                with AppServerClient(path, Path(handle.name)) as client:
                    models = client.list_models()
        except Exception:
            continue
        for item in models or ():
            if not isinstance(item, dict):
                continue
            if str(item.get("model") or item.get("id") or "") == model_id:
                serving.append(path)
                break
    return tuple(serving)
