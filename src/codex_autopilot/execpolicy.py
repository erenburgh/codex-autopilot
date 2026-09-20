"""The one managed block Autopilot writes into the Codex execpolicy.

Why the rule exists is in ``scripts/register_execpolicy.py``, which is the
command the installer runs. Why it lives *here* is uninstall: the block is
written outside Autopilot's own directory, into a file that belongs to
Codex, and something has to take it back out. ``uninstall --yes`` removed
the plugins, the launch agent and the runtime and left two standing
``decision="allow"`` rules behind, pointing at a script that no longer
existed - a grant outliving the thing it was granted to.

Both the writer and the remover therefore read the same marker from one
place. A second copy of it in the uninstall path would drift on the first
day someone changed the wording, and the drift would be silent: a stale
block is not an error, it is just a permission nobody asked for any more.

Writing stays in the installer script that performs it. A ``register`` here
would have no production caller - the installer runs a script, not the
runtime - and exempting it from the unreachable-contract detector would
mean exempting the bare name ``register`` for the whole tree.
"""

from __future__ import annotations

import os
from pathlib import Path


MARKER = "# codex-autopilot (managed): the plugin's own script, direct argv"
# The marker written by installs before the harness switched to English. It
# is still recognized so an upgrade removes that block instead of stacking
# a second one under it.
LEGACY_MARKERS = (
    "# codex-autopilot (managed): собственный скрипт плагина, прямой argv",
)
# Only the initiating turn's commands. Everything else - devops-*,
# uninstall, hook, relay-* - still asks.
ALLOWED_COMMANDS = ("start-skill", "timeline")


def rules_path(codex_home: Path | None = None) -> Path:
    """The execpolicy file the installer writes, under the Codex home."""

    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return Path(home).expanduser() / "rules" / "default.rules"


def strip_managed(existing: str) -> str:
    """Remove the previous managed block without touching foreign rules."""

    kept: list[str] = []
    inside = False
    for line in existing.splitlines():
        if line.strip() == MARKER or line.strip() in LEGACY_MARKERS:
            inside = True
            continue
        if inside:
            if line.startswith("prefix_rule(") and "codex-autopilot" in line:
                continue
            inside = False
        kept.append(line)
    return "\n".join(kept).rstrip("\n")


def remove(path: Path) -> bool:
    """Take the managed block back out. Returns whether anything was there.

    A rules file the user or Codex also wrote into is rewritten without our
    block and kept. A file that held nothing but our block is deleted,
    because it did not exist before the install either - but an empty file
    that was already there is left alone, since we cannot tell it apart
    from one the user emptied themselves.
    """

    if not path.is_file():
        return False
    existing = path.read_text(encoding="utf-8")
    if MARKER not in existing and not any(
        marker in existing for marker in LEGACY_MARKERS
    ):
        return False
    body = strip_managed(existing)
    if body.strip():
        path.write_text(body + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True
