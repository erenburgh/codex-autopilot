"""A permission request in a turn: never answered, never retried, always looked at.

The dispatcher never answers an approval - that is her boundary, and nothing
here changes it. What was wrong was the road after it. ``ApprovalRequired``
fell into the generic ``app_server_rpc_failed``: the task went to
RETRY_WAIT, the next attempt met the same request, the ceiling burned five
attempts of her limits on it, and only then did a PIPELINE ticket open. Read
in the code: the request arrives on the dispatcher's own connection
(``appserver._inspect_event``), the dispatcher's App Server exits, and the
request dies with it - there is nothing left in Desktop for her to answer.
After a plain Resume the task met the same request again: a loop.

So a permission request is its own failure code, ``approval_required``:

- it is recorded once and not counted towards the retry ceiling;
- the task is held by a stop ticket at once (``stop_kind=approval_required``)
  with the request itself in the ticket, so no retry runs while the ticket is
  open;
- the ticket goes to the on-call first, like every stop. It compares the
  request with what the run is authorized for (R4) and the permission profile
  the run uses (``stop_diagnosis``). What the run is authorized for is the
  durable authorization in run-state (``run_authorization``, R4): a request
  that falls under one of its operations is recorded as an R4 violation and
  marked ``covered_by`` - a runtime defect by rule, which may not be sent to
  her as a confirmation request (``read_engineer_outcome`` refuses that). A
  runtime that asked for more than the run needs - a wrong profile, a command
  it should not have issued - is a defect it repairs, and the ticket closes
  only with that live patch on it
  (``engineer_stop_actions.require_stop_ticket_closable``). Otherwise it hands
  the ticket to her as DANGEROUS_PERMISSION with a recommendation. The
  ticket's ``reason_code`` is that hand-up code, not a worker's own stop: the
  on-call's prompt has a paragraph of its own for this kind
  (``engineer_escalation.APPROVAL_TICKET_BRIEF``);
- her answer is applied without the loop (``owner_answers``): ``replan``
  changes the plan so the task no longer needs the operation; ``retry`` - she
  granted it herself - runs the task once more, and the same request after her
  retry is not retried a second time. Resume is a ``retry`` recorded with the
  request's signature, so the rule holds on that door too.

An on-call's own request holds no task: its ticket is anchored to the task as
context only.

A request of a thread filed at the root is told apart by the runtime, not
by the on-call's reading (the third independent check). Under placement
contract 2 a staged task's thread has ``cwd = root`` and writes only its
workspace; a command that writes a relative path meets the read-only root,
the sandbox refuses, and the turn asks. The first version gave that request
the same DANGEROUS_PERMISSION as any other and a count of every request of
the run, and left "the model wrote into the read-only root" versus "the
task needs a permission" to the on-call reading the brief. Now
``approval_class`` decides it from the request itself (the 0.153.4 App
Server schema: ``additionalPermissions.fileSystem``, ``grantRoot``,
``fileChanges``, ``cwd``, ``networkApprovalContext``):

- ``root_write``: a contract-2 thread asks to write under the root outside
  its workspace - an explicit write path there, or, with no path and no
  network in the request, a command at the root that the sandbox refused
  (codex's own ``SANDBOX_RETRY_REASON``: the relative-path case). Its own
  failure code (``approval_root_write``), its own count per run
  (``root_write_approvals_in_run``), and its reason code is
  RECOVERY_EXHAUSTED: the runtime filed the thread there, so it is a runtime
  defect of placement, and ``read_engineer_outcome`` refuses to send it to
  her as DANGEROUS_PERMISSION - as for a request the run's authorization
  covers;
- ``task_permission``: everything else, the road described above.

Conservative by construction: a request whose target cannot be read is a
task permission, and nobody answers either.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

# Fields of an approval request that differ between two asks for the same
# operation; the signature is taken without them.
_VOLATILE = frozenset(
    {"itemId", "threadId", "turnId", "callId", "id", "conversationId", "reason", "requestId"}
)
MAX_PAYLOAD_CHARS = 2_000


def approval_signature(payload: Mapping[str, Any]) -> str:
    """What was asked for, independent of which turn asked (R23 counts by it)."""

    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    stable = (
        {key: value for key, value in params.items() if key not in _VOLATILE}
        if isinstance(params, Mapping)
        else params
    )
    digest = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{method}:{digest}"


ROOT_WRITE = "root_write"
TASK_PERMISSION = "task_permission"
# The reason codex gives when its sandbox refused a command and it asks to
# run it again unsandboxed - read from the codex binary's own strings (the
# same build the probe measures). A request without a path, at the root,
# with this reason is the relative-path case; with any other reason (a
# network wish, a question the model asks) the target is not known.
SANDBOX_RETRY_REASON = "command failed; retry without sandbox?"


def _write_targets(params: Mapping[str, Any]) -> list[str]:
    """Paths the request asks to write, as the schema names them."""

    found: list[str] = []
    for holder in (params.get("additionalPermissions"), params.get("permissions")):
        file_system = holder.get("fileSystem") if isinstance(holder, Mapping) else None
        if not isinstance(file_system, Mapping):
            continue
        found.extend(str(item) for item in file_system.get("write") or () if item)
        for entry in file_system.get("entries") or ():
            if not isinstance(entry, Mapping):
                continue
            path = entry.get("path")
            if entry.get("access") == "write" and isinstance(path, Mapping) and path.get("type") == "path":
                found.append(str(path.get("path") or ""))
    if params.get("grantRoot"):
        found.append(str(params["grantRoot"]))
    for key in ("fileChanges", "changes"):
        if isinstance(params.get(key), Mapping):
            found.extend(str(item) for item in params[key])
    return [item for item in found if item.strip()]


def _asks_network(params: Mapping[str, Any]) -> bool:
    extra = params.get("additionalPermissions") or params.get("permissions")
    return bool(params.get("networkApprovalContext")) or bool(
        isinstance(extra, Mapping) and extra.get("network")
    )


def approval_class(cfg: Any, session: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """``root_write`` or ``task_permission`` - see the module docstring."""

    from .placement_contract import CONTRACT

    if session.get("placement_contract") != CONTRACT:
        return TASK_PERMISSION
    root = Path(cfg.root).expanduser().resolve(strict=False)
    workspace = Path(str((session.get("descriptor") or {}).get("cwd") or root)).expanduser().resolve(strict=False)
    params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
    if workspace == root or _asks_network(params):
        return TASK_PERMISSION
    base = Path(str(params.get("cwd") or root)).expanduser()
    base = base if base.is_absolute() else root / base

    def in_root_not_workspace(value: str) -> bool:
        candidate = Path(value).expanduser()
        candidate = (candidate if candidate.is_absolute() else base / candidate).resolve(strict=False)
        return candidate.is_relative_to(root) and not candidate.is_relative_to(workspace)

    targets = _write_targets(params)
    if targets:
        return ROOT_WRITE if any(in_root_not_workspace(item) for item in targets) else TASK_PERMISSION
    command = str(payload.get("method") or "") in {"item/commandExecution/requestApproval", "execCommandApproval"}
    refused = str(params.get("reason") or "").startswith(SANDBOX_RETRY_REASON)
    at_root = bool(params.get("cwd")) and in_root_not_workspace(str(params.get("cwd")))
    return ROOT_WRITE if command and refused and at_root else TASK_PERMISSION


def approvals_in_run(cfg: Any, state: Any, *, approval_kind: str | None = None) -> int:
    """Permission-request stops this run already filed - of one class when named."""

    from .pipeline_engineer import PipelineIncidentStore

    return sum(
        1
        for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents") or ()
        if (item.get("system_state") or {}).get("stop_kind") == "approval_required"
        and str((item.get("system_state") or {}).get("run_id") or "") == str(getattr(state, "run_id", "") or "")
        and (approval_kind is None or (item.get("system_state") or {}).get("approval_class") == approval_kind)
    )


def bounded_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= MAX_PAYLOAD_CHARS:
        return json.loads(text)
    return {"method": payload.get("method"), "truncated": text[:MAX_PAYLOAD_CHARS]}


def record_approval_required(
    cfg: Any,
    reservation_token: str,
    payload: Mapping[str, Any],
    *,
    thread_id: str | None,
    turn_id: str | None,
    owner: str,
    now_epoch: int | None = None,
) -> str | None:
    """Record the request once, hold the task by a ticket, route it to the on-call."""

    from .blocked_runs import stop_run
    from .lifecycle_base import DesktopLifecycleError, _session_by_token
    from .lifecycle_failures import record_desktop_failure
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore, utc_now

    signature = approval_signature(payload)
    kind = approval_class(cfg, _session_by_token(StateStore(cfg.state_dir).load(), reservation_token), payload)
    try:
        record_desktop_failure(
            cfg,
            reservation_token,
            reason=f"the turn asked for a permission the dispatcher never answers ({kind}): {signature}",
            failure_code="approval_root_write" if kind == ROOT_WRITE else "approval_required",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id or None,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
    except DesktopLifecycleError:
        # Already reconciled by someone else: the ticket below is still due -
        # a request nobody looked at may not vanish with the session.
        pass
    from .run_authorization import covering_operation, ensure_recorded

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        task_id = str(session.get("task_id") or "")
        engineer = session.get("kind") == "pipeline_engineer"
        now = utc_now()
        # R4: the request is read against the durable authorization in
        # run-state. A run armed before the list was recorded gets it now -
        # it was authorized all along, only the record was missing.
        authorization = ensure_recorded(cfg, state, at=now, granted_by="backfill_at_first_request")
        covered = covering_operation(authorization, payload)
        covered_by = (
            {"operation": covered, "version": authorization.get("version")} if covered else None
        )
        if covered_by:
            # The run already holds this permission: asking for it again is
            # what R4 forbids, and the asker is the runtime - a defect for
            # the on-call to repair, never a question for her.
            from .rules import record_violation

            record_violation(
                cfg.state_dir,
                "R4",
                detail=(
                    f"{task_id} {session.get('kind')} asked for {signature}, covered by "
                    f"{covered} (durable authorization v{authorization.get('version')})"
                ),
            )
        incident_id = stop_run(
            cfg,
            state,
            stop_kind="approval_required",
            phase="APPROVAL_REQUIRED",
            reason=(
                f"{task_id} {session.get('kind')}: the turn asked for a permission "
                f"({payload.get('method')}); nobody answers it for her"
            ),
            summary=(
                f"A {session.get('kind')} turn of {task_id} stopped on a permission request. "
                "The on-call compares it with what the run is authorized for."
            ),
            at=now,
            task_ids=() if engineer else (task_id,),
            context_task_id=task_id if engineer else "",
            system_state={
                "held": not engineer,
                # A request from the root the runtime filed the thread at is
                # the runtime's defect: it goes up only as RECOVERY_EXHAUSTED.
                "reason_code": "RECOVERY_EXHAUSTED" if kind == ROOT_WRITE else "DANGEROUS_PERMISSION",
                "approval_class": kind,
                "approval": bounded_request(payload),
                "approval_signature": signature,
                "covered_by": covered_by,
                "session_kind": str(session.get("kind") or ""),
                "reservation_token": reservation_token,
                # How often: a thread filed at the root (placement_contract 2)
                # whose shell writes a relative path meets the read-only root
                # and asks - the independent check's risk, counted per run
                # rather than trusted to the prompt's workdir line.
                "approvals_in_run": approvals_in_run(cfg, state) + 1,
                "root_write_approvals_in_run": (
                    approvals_in_run(cfg, state, approval_kind=ROOT_WRITE) + (1 if kind == ROOT_WRITE else 0)
                ),
                "placement_contract": session.get("placement_contract"),
            },
        )
        store.save(state)
    return incident_id
