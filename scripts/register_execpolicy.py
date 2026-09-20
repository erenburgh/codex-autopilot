#!/usr/bin/env python3
"""Register Autopilot's own script in the Codex execpolicy.

Why this exists. Codex asks permission for a command that is not in the
execpolicy. For Autopilot that means launching its own installed script may
hit a native dialog - and by its own rule the dispatcher never answers
approvals. The request hangs in a task the user is not looking at, the
initiating turn stays silent, and the run does not start.

What exactly was measured on a live machine:

- ``codex execpolicy check`` allows the command when it comes as direct argv;
- the same command inside ``/bin/zsh -lc "..."`` matches no rule, because
  rules are matched by argv tokens and the whole shell string is one token.

Two consequences. The SKILL requires launching the script without a shell
wrapper, and exactly the rule that covers such a launch is created here.

Only the initiating turn's commands are allowed. Everything else -
``devops-*``, ``uninstall``, ``hook``, ``relay-*`` - still asks.

The script path is versioned, so the block is rewritten on every install:
old Autopilot lines are removed, new ones added. Rules written by the user
or by Codex itself are not touched.

The marker and the rule text live in ``codex_autopilot.execpolicy``, not
here: ``uninstall`` has to remove exactly what this writes, and a copy of
the marker in each place would drift the first time the wording changed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_autopilot.execpolicy import (  # noqa: E402,F401
    ALLOWED_COMMANDS,
    LEGACY_MARKERS,
    MARKER,
    strip_managed,
)


def build_block(script: str, commands: tuple[str, ...] = ALLOWED_COMMANDS) -> str:
    lines = [MARKER]
    for command in commands:
        lines.append(f'prefix_rule(pattern=[{script!r}, {command!r}], decision="allow")')
    return "\n".join(lines)


def register(script: str, rules_path: Path, commands: tuple[str, ...] = ALLOWED_COMMANDS) -> str:
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    existing = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    body = strip_managed(existing)
    block = build_block(script, commands)
    text = (body + "\n\n" if body else "") + block + "\n"
    rules_path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, help="installed skill-side launcher path")
    parser.add_argument("--rules", required=True, type=Path, help="Codex execpolicy rules file")
    args = parser.parse_args()
    register(args.script, args.rules)
    print(
        f"Execpolicy: allowed {args.script} "
        f"{{{', '.join(ALLOWED_COMMANDS)}}} in {args.rules}"
    )
    print(
        "Execpolicy: 'codex-autopilot uninstall --yes' takes this block back out "
        f"of {args.rules}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
