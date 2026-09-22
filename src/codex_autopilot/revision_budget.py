"""What the revision tally was spent on, and when it stops counting.

The hiring ladder bounds how many times one problem may be retried: two
revisions per hire, then a re-hire at a higher reasoning effort, then a
stop. The ceiling is the point of it. A task that keeps failing under an
unchanged contract must run out, or acceptance moves to meet the work.

But a replanner can insert a prerequisite beneath a task, and then the
task is not the same problem any more.

Measured on a live run of 22 Sep 2026. M11 spent four revisions and a
re-hire against its original prerequisites, failing each time on a hole
in what it depended upon. Its worker finally requested a plan change; the
replanner inserted M11A beneath it, M11A closed that hole, was verified
independently and promoted, and M11's ``depends_on`` became a different
set entirely. M11 then got exactly ONE attempt at the new problem - on a
new tree, with the old obstacle gone - before the ladder, still holding
the tally from work that no longer existed, declared it exhausted and
stopped the run.

So the tally carries its grounds. Only a change we can actually see -
the prerequisites the attempts were made against - starts it over;
everything else leaves the ceiling exactly where it was.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def basis_for(graph_version: int, depends_on: Iterable[str]) -> dict[str, Any]:
    """What a revision attempt is being made against, recorded with it."""

    return {"graph_version": int(graph_version), "depends_on": [str(item) for item in depends_on]}


def recorded_depends_on(basis: Any) -> list[str] | None:
    """The prerequisites a tally was spent against, or None if unrecorded.

    An unrecorded basis is not an invitation to forgive: attempts that
    predate this bookkeeping stay charged, because the conservative
    reading is the one that keeps the ceiling.
    """

    if not isinstance(basis, Mapping):
        return None
    return [str(item) for item in basis.get("depends_on") or ()]


def premises_changed(basis: Any, depends_on: Iterable[str]) -> bool:
    """Whether the task now rests on something other than what it was tried on."""

    before = recorded_depends_on(basis)
    if before is None:
        return False
    return before != [str(item) for item in depends_on]
