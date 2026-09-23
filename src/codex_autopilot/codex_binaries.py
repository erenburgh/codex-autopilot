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

from pathlib import Path
import re
import shutil
import subprocess


# Desktop keeps its binary at a stable path inside the bundle, so pointing
# at it follows Desktop's own updates instead of pinning one version.
DESKTOP_BUNDLED = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


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
    if DESKTOP_BUNDLED.is_file():
        candidates.append(str(DESKTOP_BUNDLED))
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
