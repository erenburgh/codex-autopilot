"""A fresh hire bought by a runtime patch lives exactly as long as the patch.

A task at the top of its hiring ladder returns only after a change that
touched the cause of its refusals (R23). One such change is a runtime patch
on the acceptance path. The first version of this rule read the patch from
the ticket (``incident.runtime_patches``) - and that record is written when
the patch is STAGED, before anything is installed. The independent check
measured both ways that goes wrong:

- a patch on verification.py recorded and staged, then withdrawn with
  ``devops-revert-runtime-patch``: the return still granted a fresh hire
  (rehires 3 -> 4) on grounds ``runtime_patch_ids=['p-w']`` - a patch that
  never reached the runtime;
- a patch the wake-up refused (the tree moved under it, or this is not an
  installation): the task had already been returned with a fresh budget,
  and once the drain ended it ran on the old code. The refusal filed a
  ticket and revoked nothing.

And "on the acceptance path" was read from the module names the ticket
recorded, over a list that also held lifecycle_completion.py, models.py
and rules.py - a patch there bought a fresh hire with no change to how the
work is judged.

So here:

- what a patch changed on the acceptance path is read from the staged pair
  itself - the text each module was proven against and the text it becomes
  (``runtime_install.stage_proven_patch`` keeps both) - over the explicit
  list in ``engineer_authority`` (whole acceptance modules, and the
  verifier's own parts of the prompt builders), compared as syntax trees;
- only a patch that is still staged or installed counts
  (``runtime_install.patch_status``); withdrawn, refused or reverted, it
  never did;
- when a patch goes away after it bought a hire, the hire is revoked
  (``revoke_grants``): the tally goes back to what it was, and a task that
  has not started since goes back to BLOCKED - held by a ticket, so the
  on-call looks at it again. It cannot have started on the old code in
  between: while a patch is staged the run drains and nothing is reserved,
  and the revocation happens in the same transaction that takes the patch
  away.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Iterable

from .runtime_install import INSTALLED, PENDING, patch_root, patch_status

LIVE_PATCH_STATUSES = frozenset({PENDING, INSTALLED})


def patch_is_live(state_dir: Path, patch_id: str) -> bool:
    """Staged or installed, and not taken back."""

    return patch_status(state_dir, patch_id) in LIVE_PATCH_STATUSES


def _entry(state_dir: Path, patch_id: str) -> Path | None:
    for where in (PENDING, INSTALLED):
        candidate = patch_root(state_dir) / where / patch_id
        if (candidate / "patch.json").is_file():
            return candidate
    return None


def _tests_phase(test: ast.AST, branch: str) -> bool:
    """``phase == "<branch>"`` anywhere in an if's condition."""

    for node in ast.walk(test):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "phase"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == branch
        ):
            return True
    return False


def _shape(text: str, name: str | None, branch: str | None) -> list[str] | None:
    """The syntax of a module, a definition, or a definition's phase branch.

    None when the text does not parse: such a source can prove nothing.
    """

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    if name is None:
        return [ast.dump(tree)]
    shapes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != name:
            continue
        if branch is None:
            shapes.append(ast.dump(node))
            continue
        for inner in ast.walk(node):
            # Only the branch's own body: the orelse of the same ``if`` is
            # another phase's prompt.
            if isinstance(inner, ast.If) and _tests_phase(inner.test, branch):
                shapes.append("\n".join(ast.dump(item) for item in inner.body))
    return shapes


def acceptance_path_changes(state_dir: Path, patch_id: str) -> list[str]:
    """What this live patch changed on the acceptance path; [] when nothing.

    Read from the staged pair of texts, never from the ticket's record.
    """

    from .engineer_authority import LADDER_RESET_DEFINITIONS, LADDER_RESET_MODULES

    if not patch_is_live(state_dir, patch_id):
        return []
    entry = _entry(state_dir, patch_id)
    if entry is None or not (entry / "modules").is_dir():
        return []
    changed: list[str] = []
    for source in sorted((entry / "modules").glob("*.py")):
        module = source.name
        after = source.read_text(encoding="utf-8")
        original = entry / "originals" / module
        before = original.read_text(encoding="utf-8") if original.is_file() else None
        if before is None:
            # A new module changes nothing by itself: whatever uses it is
            # an existing module, and that one is compared.
            continue
        if module in LADDER_RESET_MODULES:
            old, new = _shape(before, None, None), _shape(after, None, None)
            if old is not None and new is not None and old != new:
                changed.append(module)
            continue
        for listed, name, branch in LADDER_RESET_DEFINITIONS:
            if listed != module:
                continue
            old, new = _shape(before, name, branch), _shape(after, name, branch)
            if old is not None and new is not None and old != new:
                changed.append(f"{module}:{name}" + (f"[phase={branch}]" if branch else ""))
    return changed


def revoke_grants(
    cfg: Any,
    plan: Any,
    state: Any,
    patch_ids: Iterable[str],
    *,
    reason: str,
    at: str,
) -> list[str]:
    """Revoke every fresh hire that rested only on these patches.

    The caller holds the run's transaction and saves ``state``. Returns the
    tasks moved back to BLOCKED; the caller makes sure a ticket holds them
    (``hold_revoked``). A task already at work keeps its current attempt -
    it started on the patched code - but not the budget: its next REVISE
    finds the ladder spent again.
    """

    from .lifecycle_base import PENDING_SESSION_STATUSES, _append_event
    from .pipeline_engineer import PipelineIncidentStore
    from .task_state import TaskState, transition_task

    gone = {str(item) for item in patch_ids}
    store = PipelineIncidentStore(cfg.state_dir)
    blocked: list[str] = []
    for incident in store.load().get("incidents") or ():
        revoked = {
            (str(item.get("task_id")), str(item.get("return_at")))
            for item in incident.get("revoked_grants") or ()
        }
        for entry in incident.get("returns") or ():
            ids = [str(item) for item in (entry.get("grounds") or {}).get("runtime_patch_ids") or ()]
            task_id = str(entry.get("task_id") or "")
            if not ids or not gone.intersection(ids):
                continue
            if (task_id, str(entry.get("at"))) in revoked:
                continue
            if any(item not in gone and patch_is_live(cfg.state_dir, item) for item in ids):
                continue  # another patch of the same grant still stands
            grant = entry.get("grant") or {}
            if "rehires_before" in grant and int(state.task_rehires.get(task_id, 0)) == int(
                grant.get("rehires_now", -1)
            ):
                state.task_rehires[task_id] = int(grant["rehires_before"])
            basis = dict((state.task_revision_basis or {}).get(task_id) or {})
            if basis.pop("granted_on", None) is not None:
                state.task_revision_basis[task_id] = basis
            at_work = task_id in state.active_task_ids or any(
                str(item.get("task_id") or "") == task_id
                and item.get("status") in PENDING_SESSION_STATUSES
                and item.get("kind") != "pipeline_engineer"
                for item in state.worker_sessions
            )
            moved = (
                not at_work
                and task_id in plan.task_map
                and state.task_states.get(task_id) == str(entry.get("to") or "")
            )
            if moved:
                state.task_states = transition_task(
                    plan, state.task_states, task_id, TaskState.BLOCKED
                )
                blocked.append(task_id)
            record = {
                "task_id": task_id,
                "return_at": str(entry.get("at")),
                "runtime_patch_ids": ids,
                "reason": reason,
                "blocked_again": moved,
                "at": at,
            }
            store.record_engineer_action(
                str(incident["incident_id"]),
                field="revoked_grants",
                event="fresh_hire_revoked",
                entry=record,
                at=at,
                holder_only=False,
            )
            _append_event(
                state,
                "fresh_hire_revoked",
                {
                    "operation_id": f"fresh-hire-revoked:{task_id}",
                    "task_id": task_id,
                    "attempt": max(1, int(state.task_attempts.get(task_id, 0))),
                    "reservation_token": f"fresh-hire-revoked:{task_id}",
                },
                at,
                detail=json.dumps(record, sort_keys=True),
            )
    return blocked


def hold_revoked(
    cfg: Any,
    state: Any,
    tasks: Iterable[str],
    *,
    stop_kind: str,
    reason: str,
    at: str,
) -> str | None:
    """A task sent back to BLOCKED is held by a ticket, never left waiting for nobody.

    An open stop ticket that already holds it is enough (the on-call who
    withdrew its own patch cannot close it while the task is BLOCKED);
    otherwise a stop is filed for the on-call. Returns the new ticket, if any.
    """

    from .blocked_runs import stop_run
    from .engineer_reservation import _open
    from .pipeline_engineer import STOP_CODE_PREFIX

    held = {
        str(task)
        for item in _open(cfg)
        if str(item.get("code") or "").startswith(STOP_CODE_PREFIX)
        for task in item.get("affected_task_ids") or ()
    }
    loose = tuple(str(task) for task in tasks if str(task) not in held)
    if not loose:
        return None
    return str(
        stop_run(
            cfg,
            state,
            stop_kind=stop_kind,
            phase="BLOCKED",
            reason=reason,
            summary=(
                "A fresh hire rested on a runtime patch that was not installed; "
                "the task waits for the on-call again."
            ),
            at=at,
            task_ids=loose,
            system_state={"revoked_fresh_hire": True},
        )
    )
