"""Notification of readiness: the only path we have.

Measured on a live App Server, not assumed:

- ``initialize`` returns no capability list at all - not one declared API
  about unread state, badges or notifications;
- ``thread/metadata/update`` accepts only ``projectId``. Control
  experiment: the same field with its previous value passes, while
  ``name``, ``title``, ``threadName``, ``section``, ``sectionEnteredAt``,
  ``agentNickname`` and ``agentRole`` are rejected with the same "must
  include at least one field";
- the methods ``thread/rename``, ``thread/setName``, ``thread/title/update``,
  ``thread/markUnread``, ``thread/setUnread``, ``thread/unread/update``,
  ``thread/notify`` and ``notification/create`` do not exist.

So a thread can neither be marked read nor renamed after creation. The
"unread" state belongs to the Desktop interface and is not ours from
outside.

But the dispatcher is an ordinary local process on the user's machine,
and the system banner is available to it without anyone's API. That is
the answer to the real question: not "show a badge" but "say when it is
ready".

Off by default. A notification is a side effect on the human's machine,
and it is enabled explicitly.
"""

from __future__ import annotations

import shutil
import subprocess

# The text goes as arguments, not inside the script: task titles carry
# quotes, brackets and Cyrillic, and string concatenation here would sooner
# or later turn into an injection or an AppleScript syntax error.
_SCRIPT = """on run argv
    display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run"""

_MAX_FIELD_CHARS = 200


def notify(cfg, title: str, subtitle: str, message: str) -> bool:
    """Show a system banner. Never raises and never waits.

    Returns True only if the banner was really sent. A notification may
    neither delay the pipeline nor fail it: a failure here means only that
    the human did not see the hint.
    """

    if not getattr(getattr(cfg, "runtime", None), "desktop_notifications", False):
        return False
    binary = shutil.which("osascript")
    if not binary:
        return False
    try:
        subprocess.run(
            [binary, "-", _clip(title), _clip(subtitle), _clip(message)],
            input=_SCRIPT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return True


def _clip(value: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[: _MAX_FIELD_CHARS - 1] + "…"
