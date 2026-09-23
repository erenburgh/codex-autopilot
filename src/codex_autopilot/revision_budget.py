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


def block_on_exhausted_ladder(
    cfg,
    plan,
    state,
    task_id: str,
    *,
    session,
    at: str,
    hires: int,
    effort: str,
    used: int,
    maximum: int,
    transition,
    append_event,
) -> None:
    """The ladder is spent: stop the task, and tell someone.

    Re-hiring at the next reasoning step is the answer to a task that keeps
    coming back revised. When even the top of the ladder cannot close it, the
    task stops - and that stop is a decision for the owner, because what is
    left is a judgement about the work, which no automaton may make for them.

    It used to stop in silence: a state on disk, a line in run state, and
    nothing in the incident journal. Then (0.13.0) it opened a ticket but did
    not route it, so the neighbours would keep the dispatcher - and the
    engineer never came. Now the ticket goes to the on-call like any other:
    it works next to the neighbours, looks at whether the refusals are about
    the work or about the gate, rubric or runtime, and hands the owner a
    diagnosis only when the judgement really is hers.
    """

    import json

    from .blocked_runs import stop_run
    from .task_state import TaskState

    if state.task_states[task_id] == TaskState.REVISION_REQUIRED.value:
        state.task_states = transition(plan, state.task_states, task_id, TaskState.BLOCKED)
    append_event(
        state,
        "hiring_ladder_exhausted",
        session,
        at,
        detail=json.dumps(
            {
                "revision_attempts": used,
                "max_revision_attempts": maximum,
                "hires": hires,
                "effort": effort,
            },
            sort_keys=True,
        ),
    )
    stop_run(
        cfg,
        state,
        stop_kind="ladder_exhausted",
        phase="BLOCKED",
        reason=(
            f"{task_id} exhausted the hiring ladder: {hires} hire(s) up to "
            f"effort {effort}, {used} revision attempt(s)"
        ),
        summary=(
            f"{task_id} was re-hired up to effort {effort} and still came back "
            "revised. Whether the work is good enough is yours to judge."
        ),
        at=at,
        task_ids=(task_id,),
        system_state={"hires": hires, "effort": effort, "revision_attempts": used},
    )
