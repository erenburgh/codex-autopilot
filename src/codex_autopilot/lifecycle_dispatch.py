from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .appserver import (
    AppServerClient,
    AppServerRpcError,
    ApprovalRequired,
    PauseRequested,
    ProjectRootDrift,
    final_agent_message,
    is_rate_limit_error,
)
from .artifact_staging import ArtifactStagingError, ArtifactStagingStore
from .config import Config
from .hook_trust import require_trusted_stop_hook_for_config
from .preflight import installed_plugin_root
from .project_association import (
    project_root_authorization_statement,
    project_root_mutation_authorized,
)
from .placement_contract import Placement, roots_within, session_cwd, session_profile, thread_placement
from .resources import ResourceLockCoordinator
from .run_state import RunState, StateStore, utc_now

from .lifecycle_base import (
    CompletionOutcome,
    DesktopLifecycleError,
    LaunchDescriptor,
    WorkerProtocolError,
    _append_event,
    _bind_resource_identity,
    _client_process_exited,
    _dispatcher_owns_reservation,
    _materialize,
    _require_desktop_owned,
    _require_relay_executor,
    _session_by_token,
    _thread_cwd,
    _wait_for_dispatcher_ownership,
    acknowledge_desktop_send,
)
from .lifecycle_failures import (
    _record_app_server_create_failure,
    _record_created_app_server_ambiguity,
    record_desktop_failure,
)


def app_server_creation_contract(
    cfg: Config,
    descriptor: LaunchDescriptor,
    placement: Placement | None = None,
) -> dict[str, Any]:
    """Return the exact project-scoped create contract (placement_contract).

    ``cwd`` is where Desktop files the thread, ``runtimeWorkspaceRoots``
    where it may write: the root and the staged workspace under contract 2,
    with the task's staged profile. ``placement`` - the one actually used.
    """

    if placement is None:
        placement = thread_placement(cfg, _descriptor_workspace(cfg, descriptor), descriptor.kind)
    params: dict[str, Any] = {
        "cwd": str(placement.cwd),
        "permissions": placement.permission_profile,
        "ephemeral": False,
    }
    if placement.workspace_roots is not None:
        params["runtimeWorkspaceRoots"] = [str(item) for item in placement.workspace_roots]
    if cfg.desktop.project_id:
        params["projectId"] = cfg.desktop.project_id
    if descriptor.model:
        params["model"] = descriptor.model
    contract: dict[str, Any] = {
        "method": "thread/start",
        "params": params,
        "name": descriptor.title,
        "placement_contract": placement.contract,
    }
    if cfg.desktop.project_id:
        contract["project_root_precondition"] = {
            "method": "project/update-if-missing",
            "params": {
                "projectId": cfg.desktop.project_id,
                "root": str(cfg.root),
            },
        }
    return contract


def _descriptor_workspace(cfg: Config, descriptor: LaunchDescriptor) -> Path:
    """Authenticate a descriptor cwd against canonical or staged state."""

    workspace = Path(descriptor.cwd).expanduser().resolve(strict=False)
    if workspace == cfg.root:
        return workspace
    try:
        staged = ArtifactStagingStore(cfg.root, cfg.state_dir).load(descriptor.task_id)
    except ArtifactStagingError as exc:
        raise DesktopLifecycleError(
            f"descriptor references an unavailable staged workspace: {exc}"
        ) from exc
    if workspace != staged.workspace:
        raise DesktopLifecycleError(
            "descriptor cwd does not match the task's durable staged workspace"
        )
    return workspace

def _project_root_mutation_authorized(cfg: Config) -> bool:
    """R6: may the saved project's roots be edited in this run.

    A short memory session of its own is opened: the creation path holds no
    ProjectMemory, and opening one for a single lookup on every launch costs
    more than asking once here. Any read error is "no": a closed refusal
    must not depend on the store being available.
    """

    from .memory import ProjectMemory

    if not cfg.desktop.project_id:
        return False
    try:
        memory = ProjectMemory(cfg.root)
    except Exception:
        return False
    return project_root_mutation_authorized(
        memory, cfg.desktop.project_id, cfg.root
    )


def create_desktop_thread_via_app_server(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    at: str | None = None,
    now_epoch: int | None = None,
    relay_executor_thread_id: str | None = None,
    dispatcher_pid: int | None = None,
    connected_client: AppServerClient | None = None,
) -> dict[str, Any]:
    """Create a persistent canonical-cwd task through the local dispatcher.

    One bounded App Server connection owns ``thread/start``, ``turn/start``,
    and the authoritative completion wait for this task. The outer v0.7-style
    dispatcher closes that process before it advances to a successor. No model
    continuation or Codex App task API participates.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if not _dispatcher_owns_reservation(
            session,
            dispatcher_pid=dispatcher_pid,
        ):
            require_trusted_stop_hook_for_config(cfg)
        if session.get("status") != "CREATE_REQUESTED":
            raise DesktopLifecycleError(
                "App Server create was already claimed or the reservation is not launchable"
            )
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        workspace = _descriptor_workspace(cfg, descriptor)
        session["status"] = "RELAYING"
        session["creation_transport"] = "app_server_thread_start"
        session["app_server_creation_contract"] = app_server_creation_contract(
            cfg, descriptor
        )
        _append_event(state, "app_server_create_claimed", session, utc_now())
        store.save(state)

    client: Any = None
    thread_id = ""
    create_invoked = False
    actual_cwd: Path | None = None
    actual_name: str | None = None
    actual_project_id: str | None = None
    log_path = cfg.state_dir / "logs" / f"app-server-create-{reservation_token}.jsonl"
    owns_client = connected_client is None
    # A server this function launches itself defines the task's staged
    # profile when the thread will run under it (isolation_probe).
    from .isolation_probe import server_overrides

    overrides = server_overrides(cfg, session) if owns_client else ()
    client_context = (
        client_factory(cfg.desktop.binary, log_path, **({"config_overrides": overrides} if overrides else {}))
        if owns_client
        else nullcontext(connected_client)
    )
    try:
        with client_context as connected:
            client = connected
            profiles = client.list_permission_profiles(workspace)
            allowed = {
                str(item.get("id"))
                for item in profiles
                if item.get("allowed") is not False and item.get("id")
            }
            if cfg.desktop.permission_profile not in allowed:
                raise DesktopLifecycleError(
                    "configured permission profile is unavailable to App Server create"
                )
            if cfg.desktop.project_id:
                try:
                    project = client.ensure_project_root(
                        cfg.desktop.project_id,
                        cfg.root,
                        authorized=_project_root_mutation_authorized(cfg),
                    )
                except ProjectRootDrift as drift:
                    # R6 closed refusal. Creation was not called yet, so the
                    # exception goes to the definitive branch: one stable
                    # ticket opens, the task goes to RETRY_WAIT, and there is
                    # no second creation attempt. A silent edit of the saved
                    # project's roots no longer happens here.
                    raise DesktopLifecycleError(
                        f"{drift} — Autopilot does not change a saved project on "
                        "its own. To authorize this exact change, record the "
                        "user Decision: "
                        f"{project_root_authorization_statement(cfg.desktop.project_id, cfg.root)!r}"
                    ) from drift
                if str(project.get("id") or "") != cfg.desktop.project_id:
                    raise DesktopLifecycleError(
                        "configured App Server project could not be verified"
                    )
            # Everything that can fail BEFORE the request is sent is computed
            # before the flag is raised. `installed_plugin_root` used to sit
            # among the call arguments: it failed after `create_invoked =
            # True` although the request never went out. The failure became
            # UNKNOWN and produced an AMBIGUOUS_SIDE_EFFECT ticket with no way
            # out - while the dispatcher log held not one `thread/start`.
            plugin_root = installed_plugin_root(cfg.skill_path)
            # The isolation record is measured before this server was
            # launched (isolation_probe.server_overrides, by the dispatcher):
            # a staged profile must be defined at launch, so a probe on this
            # connection could not have served its own verdict.
            placement = thread_placement(cfg, workspace, descriptor.kind, available_profiles=allowed)
            create_invoked = True
            started = client.start_thread(
                cwd=placement.cwd,
                workspace_roots=placement.workspace_roots,
                permission_profile=placement.permission_profile,
                # v0.7 invariant: create the task in the saved project, with a
                # cwd that is already one of that project's durable roots.
                project_id=cfg.desktop.project_id,
                model=descriptor.model,
                plugin_root=plugin_root,
                ephemeral=False,
                project_memory=(session.get("kind") != "plan_verifier"),
                # v0.7 passed no threadSource at all, and its tasks appeared
                # in the project sidebar as ordinary threads.
                # "agent_created_thread" marks a thread as agent-created: the
                # app shows it as created in another application and demands
                # a manual takeover. Exactly this parameter distinguished 0.8
                # from the working 0.7.
            )
            thread = started.get("thread") or {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise DesktopLifecycleError("App Server thread/start returned no thread id")
            if not roots_within(started.get("runtimeWorkspaceRoots"), placement.workspace_roots):
                raise DesktopLifecycleError(
                    "App Server widened the thread's runtime workspace roots to "
                    f"{started.get('runtimeWorkspaceRoots')!r}; asked for "
                    f"{[str(item) for item in placement.workspace_roots or ()]!r} - isolation refused"
                )
            active_profile = started.get("activePermissionProfile") or {}
            if active_profile and active_profile.get("id") != placement.permission_profile:
                raise DesktopLifecycleError(
                    "App Server thread/start applied an unexpected permission profile"
                )
            client.name_thread(thread_id, descriptor.title)
            metadata = client.read_thread(thread_id)
            if str(metadata.get("id") or "") != thread_id:
                raise DesktopLifecycleError(
                    "App Server thread/read returned an unexpected created thread"
                )
            actual_cwd = _thread_cwd(metadata) or _thread_cwd(thread)
            if actual_cwd != placement.cwd:
                raise DesktopLifecycleError(
                    "App Server-created task does not use its placement contract's cwd"
                )
            actual_name = metadata.get("name")
            if actual_name != descriptor.title:
                raise DesktopLifecycleError(
                    "App Server did not preserve the deterministic task title"
                )
            raw_project_id = metadata.get("projectId")
            actual_project_id = (
                str(raw_project_id) if raw_project_id is not None else None
            )
            if cfg.desktop.project_id and actual_project_id != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server did not preserve the configured project association"
                )
            # No explicit binding after creation here, and there must be
            # none. The line above verified the thread is already in the
            # right project, or creation was refused - so
            # thread/metadata/update would bind the already bound. v0.7 does
            # not call it at all, and its tasks are visible. Measured: in the
            # 0.8.0 acceptance this call went out on each of the six workers
            # and changed nothing.
        if owns_client and (client is None or not _client_process_exited(client)):
            raise DesktopLifecycleError(
                "App Server create process did not fully exit before Desktop handoff"
            )
    except Exception as exc:
        if thread_id:
            _record_created_app_server_ambiguity(
                cfg,
                reservation_token,
                thread_id=thread_id,
                reason=str(exc),
                at=at,
            )
        else:
            definitive = (not create_invoked) or (
                isinstance(exc, AppServerRpcError) and exc.method == "thread/start"
            )
            _record_app_server_create_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                definitive=definitive,
                at=at,
                now_epoch=now_epoch,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "RELAYING" or session.get("thread_id"):
            raise DesktopLifecycleError(
                "App Server create reservation changed before acknowledgement"
            )
        session["thread_id"] = thread_id
        session["status"] = "PREPARED"
        session["actual_cwd"] = str(actual_cwd)
        session["actual_workspace_root"] = str(workspace)
        session["placement_contract"] = placement.contract
        session["placement_reason"] = placement.reason
        session["permission_profile"] = placement.permission_profile
        session["app_server_creation_contract"] = app_server_creation_contract(cfg, descriptor, placement)
        session["actual_thread_name"] = actual_name
        session["title_verification"] = "verified by App Server thread/read"
        session["actual_project_id"] = actual_project_id
        session["project_association_verification"] = (
            "verified project-scoped thread/start App Server projectId and the placement contract's cwd by "
            "thread/read; Desktop rootPaths/sidebar placement require separate verification (desktop_sidebar)"
            if cfg.desktop.project_id
            else "App Server projectId not configured"
        )
        session["create_acknowledged_at"] = timestamp
        if owns_client:
            session["prep_app_server_exited_at"] = timestamp
            session["app_server_create_exited_at"] = timestamp
            state.prep_app_server_exited_at = timestamp
        else:
            session["automatic_dispatch_connection_pid"] = dispatcher_pid
            session["app_server_create_exited_at"] = None
        state.current_thread_id = thread_id
        state.phase = "AWAITING_DESKTOP_SEND"
        state.last_error = None
        _bind_resource_identity(state, reservation_token, thread_id=thread_id)
        lifecycle_events = [
            ("app_server_thread_created", thread_id),
            ("create_acknowledged", "thread/start"),
            ("prep_completed", str(actual_cwd)),
        ]
        if cfg.desktop.project_id:
            lifecycle_events.insert(
                2,
                ("app_server_project_scoped_create", str(actual_project_id)),
            )
        lifecycle_events.insert(
            2,
            (
                "app_server_create_process_exited"
                if owns_client
                else "app_server_dispatcher_connection_retained",
                "full process exit" if owns_client else f"pid={dispatcher_pid}",
            ),
        )
        for event, detail in lifecycle_events:
            _append_event(state, event, session, timestamp, detail=detail)
        store.save(state)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    _materialize((descriptor,))
    # Desktop learns nothing of the creation: its app-server is a separate
    # process (/Applications/ChatGPT.app/.../codex app-server), ours is our
    # own, and all they share is the disk. No channel exists to tell it, so
    # «task taken up» reaches the human the only available way - as a
    # system banner.
    _notify_start(cfg, descriptor)
    return {
        "reservation_token": reservation_token,
        "thread_id": thread_id,
        "status": "PREPARED",
        "cwd": str(actual_cwd),
        "app_server_project_id": actual_project_id,
        "app_server_process_exited_at": timestamp if owns_client else None,
    }

def _thread_is_gone(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    connected_client: AppServerClient | None = None,
) -> bool:
    """The thread the reservation is bound to no longer exists on App Server.

    Checked by reading: the thread's absence is the server's answer, not an
    inference from our records. Any other read error does not count as
    disappearance, or a transient connection failure would re-create a live
    thread and fork the work.
    """

    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    thread_id = str(session.get("thread_id") or "")
    if not thread_id:
        return False
    context = (
        client_factory(cfg.desktop.binary, cfg.state_dir / "logs" / "thread-probe.jsonl")
        if connected_client is None
        else nullcontext(connected_client)
    )
    try:
        with context as client:
            client.read_thread(thread_id)
    except AppServerRpcError as error:
        return "not found" in str(error).lower()
    except Exception:
        return False
    return False


def _reset_to_create_requested(cfg: Config, reservation_token: str) -> None:
    """Unbind the reservation from a vanished thread and allow a new one."""

    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        lost = str(session.get("thread_id") or "")
        session["status"] = "CREATE_REQUESTED"
        for field in (
            "thread_id",
            "turn_id",
            "actual_cwd",
            "actual_thread_name",
            "actual_project_id",
            "app_server_project_id",
            "creation_transport",
            "app_server_create_exited_at",
            "create_acknowledged_at",
            "desktop_placement",
            "title_verification",
            "project_association_verification",
        ):
            session[field] = None
        _append_event(
            state,
            "lost_thread_recreate_requested",
            session,
            utc_now(),
            detail=f"thread {lost} no longer exists on App Server",
        )
        store.save(state)


def _creator_process_is_gone(session: Mapping[str, Any]) -> bool:
    """The process that created the thread no longer exists.

    It marks its own exit when it ends normally. If it crashed - on a
    placement gate refusal, say - there is no mark, and the next dispatcher
    cannot take the turn: the session stays unliftable forever.

    The barrier protects against exactly one thing: a second writer into the
    same thread. A dead pid proves that.
    """

    from .control import pid_alive

    pid = session.get("automatic_dispatch_connection_pid")
    if pid is None:
        pid = session.get("automatic_dispatch_pid")
    if not isinstance(pid, int):
        return False
    return not pid_alive(pid)


def _require_thread_placement(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    connected_client: AppServerClient | None = None,
    at: str | None,
    phase: str = "created",
) -> str:
    """Measure the thread's placement; a thread outside the project is an R5 defect.

    Placement is asked twice in one read: App Server's projectId and
    Desktop's own filing rule (``launch_gate.measure_placement``). The check
    before it trusted projectId alone and called INSIDE every staged worker
    of the beyondness run that Desktop showed in no project.

    It no longer stops work. It raised on anything but INSIDE, for a thread
    of any kind: with the honest rule, a Desktop build whose rule changed, a
    missing state file or a run started below a project root answer OUTSIDE
    or UNOBSERVABLE, and the on-call raised for the resulting incident met
    the same gate with its own thread - a loop nobody could leave
    (the independent check). Now the defect is recorded on the session and
    signalled once per cause as a ticket that holds nothing
    (``placement_defects``); the run goes on. ``phase`` is ``created`` right
    after thread/start and ``after_first_turn`` once the first turn has
    completed - a thread with no turn is not yet persisted (launch_gate), so
    R5's "visible within N seconds" is measured again when it is.

    There is nothing to send a binding with: thread/metadata/update
    succeeds while changing nothing, and writing into the application's
    state behind its back is how an earlier version masked a wrong
    diagnosis. The thread lands in the right project by its cwd.
    """

    from .launch_gate import INSIDE, OUTSIDE, measure_placement
    from .placement_defects import record_placement_defect, signal_earlier_outside_threads

    required = cfg.runtime.required_thread_placement
    if required == "any":
        return "any"
    if not cfg.desktop.project_id and not cfg.desktop.desktop_project_id:
        # No saved project - nothing to place into, nothing to require.
        # Checking the real Codex directory here would be a dependency on
        # the machine, not on the run. A Desktop project alone is enough to
        # measure: R5 is about her sidebar, not App Server's projectId.
        return "unconfigured"
    timestamp = at or utc_now()
    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    thread_id = str(session.get("thread_id") or "")
    if not thread_id:
        raise DesktopLifecycleError("placement gate requires a created Desktop thread")

    context = (
        client_factory(cfg.desktop.binary, cfg.state_dir / "logs" / "placement.jsonl")
        if connected_client is None
        else nullcontext(connected_client)
    )
    with context as client:
        after, observation = measure_placement(
            client, thread_id, cfg.desktop.project_id, cfg.desktop.desktop_project_id
        )
    before = str(session.get("desktop_placement") or "")
    _record_placement_outcome(
        cfg,
        reservation_token,
        before=before,
        after=after,
        at=timestamp,
        observation=observation,
        phase=phase,
    )
    satisfied = after == INSIDE or (required == "visible" and after == OUTSIDE)
    if not satisfied:
        record_placement_defect(cfg, reservation_token, after=after, observation=observation, at=timestamp)
    if phase == "created":
        signal_earlier_outside_threads(cfg, reservation_token, observation=observation, at=timestamp)
    return after


def _record_placement_outcome(
    cfg: Config,
    reservation_token: str,
    *,
    before: str,
    after: str,
    at: str,
    observation: Mapping[str, Any] | None = None,
    phase: str = "created",
) -> None:
    """R5's two facts, recorded apart: projectId set, and filed in the project."""

    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        session["desktop_placement"] = after
        if phase != "created":
            session[f"desktop_placement_{phase}"] = after
        detail = f"{before} -> {after}"
        if observation is not None:
            session["desktop_handoff_observation"] = dict(observation)
            session["app_server_project_id_ok"] = observation.get("app_server_project_id_ok")
            session["desktop_rule"] = observation.get("desktop_rule")
            ok = observation.get("app_server_project_id_ok")
            detail += (
                f"; projectId={'ok' if ok else 'mismatch' if ok is False else 'unknown'}"
                f"; sidebar={observation.get('desktop_rule') or 'none'}"
            )
        _append_event(
            state,
            "desktop_placement_verified" if phase == "created" else f"desktop_placement_{phase}",
            session,
            at,
            detail=detail,
        )
        store.save(state)


def _append_placement_failure(cfg: Config, reservation_token: str, exc: BaseException) -> None:
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _append_event(state, "desktop_placement_unmeasured", session, utc_now(), detail=str(exc)[:300])
        store.save(state)


def server_view_for_incident(
    client: Any,
    cfg: Config,
    state: RunState,
    incident: Mapping[str, Any],
) -> dict[str, Any]:
    """What the server thinks of the affected task's threads.

    The engineer needs this digest first of all: the run state says what
    Autopilot recorded, the server says what happened, and they diverge
    exactly when the dispatcher died halfway.

    The dispatcher gathers it, not the engineer. The dispatcher's connection
    is already open and needs no permissions; the engineer, fetching the
    same thing itself, left the working directory with Python and hit an
    access request Autopilot never answers on principle. Measured: two
    tickets in a row, each an interrupted turn on that request.

    Read-only and metadata only: no turns are requested, no worker
    transcripts are read.
    """

    affected = {str(item) for item in incident.get("affected_task_ids") or ()}
    threads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in state.worker_sessions:
        if str(session.get("task_id") or "") not in affected:
            continue
        thread_id = str(session.get("thread_id") or "")
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        entry: dict[str, Any] = {
            "thread_id": thread_id,
            "session_kind": session.get("kind"),
            "session_status": session.get("status"),
        }
        try:
            thread = client.read_thread(thread_id) or {}
        except Exception as error:  # the server answers with a refusal - that is a fact too
            entry["exists"] = False
            entry["server_error"] = str(error)[:200]
        else:
            entry["exists"] = True
            entry["name"] = thread.get("name")
            entry["project_id"] = thread.get("projectId")
            entry["status"] = thread.get("status")
        threads.append(entry)
    return {
        "gathered_by": "dispatcher",
        "configured_project_id": cfg.desktop.project_id,
        "threads": threads,
    }


def _pipeline_engineer_prompt_with_server_view(
    cfg: Config,
    client: Any,
    session: Mapping[str, Any],
) -> str:
    """The engineer's prompt with the server's answer already inside."""

    from .ai_studio import AIStudioRuntime, ContextBoundaryError
    from .lifecycle_reservations import pipeline_engineer_package
    from .plan import load_plan

    state = StateStore(cfg.state_dir).load()
    # The session's own ticket: with the engineer next to the run, another
    # ticket may have become first in the lane since the reservation.
    package = pipeline_engineer_package(
        cfg, state, str(session.get("incident_id") or "") or None
    )
    package["server_view"] = server_view_for_incident(
        client, cfg, state, package["incident"]
    )
    plan = load_plan(cfg.state_dir, cfg.profile)
    try:
        return AIStudioRuntime(
            plan,
            cfg.root,
            language=cfg.language,
            skill_path=cfg.skill_path,
        ).build_pipeline_engineer_prompt(
            package, reservation_token=str(session.get("reservation_token") or "")
        )
    except ContextBoundaryError as exc:
        # The reservation built this prompt without the server's view, and
        # that view is the first part cut to fit - so only a ticket or a
        # rules block that grew since lands here. The on-call cannot be
        # called; she is told, and the raise fails this session as before.
        from .engineer_reservation import hand_unpromptable_ticket_to_owner

        hand_unpromptable_ticket_to_owner(cfg, None, package["incident"], str(exc))
        raise


def causal_gate_open(
    turn: dict[str, Any] | None,
    state: RunState,
    *,
    thread_id: str,
    turn_id: str,
) -> bool:
    """Has the predecessor's turn really ended.

    A stable "completed" opens the gate, as in v0.7. "interrupted" alone is
    not enough: while the synchronous Stop hook runs, a second App Server
    sees the same turn interrupted an instant before it becomes completed -
    measured on the 0.7 run, turn 01a097aa-4832. Accepting that would open
    the gate at precisely the moment the barrier protects against.

    But an interruption journaled for this very turn is our own and final:
    the turn will never become completed.

    Measured: the replanner asked for a permission, the dispatcher answers
    no approvals, the turn stayed 'interrupted' forever. The on-call
    engineer sent to repair exactly this could not start either - its
    predecessor was the same dead turn - and opened a second ticket on top
    of the first. A 24-task run stood with zero done.
    """

    if not turn:
        return False
    status = str(turn.get("status") or "")
    if status == "completed":
        return True
    if status != "interrupted":
        return False
    return any(
        item.get("event") == "interrupt_observed"
        and str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        for item in state.lifecycle_journal
    )


def run_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    initiator_thread_id: str,
    initiator_turn_id: str,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    now_epoch: int | None = None,
    connected_client: AppServerClient | None = None,
) -> CompletionOutcome:
    """Run one reserved worker without a model-mediated relay.

    The trusted Stop hook starts this function in a detached local process. It
    waits until the causal predecessor turn is durably complete, creates the
    persistent App Server task, starts the production turn itself, waits for the
    authoritative completion event, and returns the structured result to the
    same dispatcher loop. Codex App ``create_thread`` and
    ``send_message_to_thread`` are not part of this transport.
    """
    # late import: breaks a circular module dependency
    from .lifecycle_completion import complete_desktop_worker

    _require_desktop_owned(cfg)
    owner = str(initiator_thread_id or "").strip()
    owner_turn = str(initiator_turn_id or "").strip()
    if not owner or not owner_turn:
        raise DesktopLifecycleError(
            "automatic App Server relay requires the causal predecessor thread and turn"
        )
    initial = StateStore(cfg.state_dir).load()
    session = _session_by_token(initial, reservation_token)
    _require_relay_executor(session, owner)
    dispatcher_authorized = _wait_for_dispatcher_ownership(
        cfg,
        reservation_token,
        owner_thread_id=owner,
    )
    if not dispatcher_authorized:
        require_trusted_stop_hook_for_config(cfg)
    if session.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
        if session.get("status") == "COMPLETED":
            return CompletionOutcome(False, None, (), initial.status == "DONE")
        raise DesktopLifecycleError(
            f"automatic relay cannot start from {session.get('status')!r}"
        )

    wait_log = cfg.state_dir / "logs" / f"app-server-wait-{reservation_token}.jsonl"
    deadline = time.monotonic() + cfg.desktop.reconcile_timeout_seconds
    wait_context = (
        client_factory(cfg.desktop.binary, wait_log)
        if connected_client is None
        else nullcontext(connected_client)
    )
    with wait_context as wait_client:
        while True:
            predecessor = wait_client.read_thread(owner)
            turn = next(
                (
                    item
                    for item in predecessor.get("turns") or []
                    if item.get("id") == owner_turn
                ),
                None,
            )
            if causal_gate_open(
                turn,
                StateStore(cfg.state_dir).load(),
                thread_id=owner,
                turn_id=owner_turn,
            ):
                break
            if time.monotonic() >= deadline:
                raise DesktopLifecycleError(
                    "causal predecessor did not reach durable completed state; "
                    f"last turn status: {(turn or {}).get('status')!r}"
                )
            # Once a second, not four times: read_thread pulls the thread's
            # whole history. On a live run that produced 42 MB of log in two
            # minutes of waiting.
            time.sleep(1.0)

    session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)
    if session.get("status") == "PREPARED" and _thread_is_gone(
        cfg,
        reservation_token,
        client_factory=client_factory,
        connected_client=connected_client,
    ):
        # v0.7 created a thread and used it at once - one connection, no
        # break. v0.8 creates the thread in one process, requires its full
        # exit and starts the turn from another process later. In that gap
        # the thread lives without a subscriber, and after a restart it may
        # be gone: turn/start answers "thread not found" while the
        # reservation stays bound to a dead identifier forever. A vanished
        # thread is a reason to create a new one, not a reason to stop.
        _reset_to_create_requested(cfg, reservation_token)
        session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)

    if session.get("status") == "CREATE_REQUESTED":
        create_desktop_thread_via_app_server(
            cfg,
            reservation_token,
            client_factory=client_factory,
            now_epoch=now_epoch,
            relay_executor_thread_id=owner,
            dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
            connected_client=connected_client,
        )

    # The placement gate: a task does not start working until its thread
    # reaches the required state in Desktop. An invisible task cannot be
    # opened and read, and that is the whole point of visible workers.
    _require_thread_placement(
        cfg,
        reservation_token,
        client_factory=client_factory,
        connected_client=connected_client,
        at=None,
    )

    descriptor = claim_automatic_app_server_turn(
        cfg,
        reservation_token,
        relay_executor_thread_id=owner,
        dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
    )
    state_after_create = StateStore(cfg.state_dir).load()
    session = _session_by_token(state_after_create, reservation_token)
    workspace = _descriptor_workspace(cfg, descriptor)
    thread_id = str(session["thread_id"])
    turn_id = ""
    completed_turn: dict[str, Any] | None = None
    production_log = (
        cfg.state_dir / "logs" / f"app-server-production-{reservation_token}.jsonl"
    )
    client: Any = None
    from .isolation_probe import server_overrides

    production_overrides = server_overrides(cfg, session) if connected_client is None else ()
    production_context = (
        client_factory(
            cfg.desktop.binary, production_log,
            **({"config_overrides": production_overrides} if production_overrides else {}),
        )
        if connected_client is None
        else nullcontext(connected_client)
    )
    try:
        with production_context as production_client:
            client = production_client
            # A turn starts only on a thread loaded by THIS connection. The
            # condition used to ask "is the connection our own", which is a
            # different question: the relay always passes a ready client,
            # and a thread created by the previous - dead - dispatcher stayed
            # unloaded. thread/read still returns metadata, and the refusal
            # came only from turn/start.
            #
            # Measured on M11: the replanner thread 01a0970c reads and
            # resumes, and turn/start answers "thread not found". Reproduced
            # on a throwaway thread: create, close the creating process,
            # start a turn from a new one - the same refusal.
            resumed: dict[str, Any] | None = None
            if thread_id in getattr(production_client, "subscribed_thread_ids", ()):
                thread = production_client.read_thread(thread_id)
            else:
                resumed = production_client.resume_thread(thread_id)
                thread = resumed.get("thread") or {}
            if session.get("placement_contract") == 2 and workspace != cfg.root:
                # A thread she can open can come back with wider roots
                # (isolation_guard): recorded and signalled; this turn
                # replaces them with the workspace under the staged profile.
                from .desktop_sidebar import codex_home_of
                from .isolation_guard import check_thread_roots

                check_thread_roots(
                    cfg, reservation_token, thread=thread, response=resumed, workspace=workspace,
                    codex_home=codex_home_of(production_client),
                )
            expected_cwd = session_cwd(cfg, session, workspace)
            if _thread_cwd(thread) != expected_cwd:
                raise DesktopLifecycleError(
                    "App Server production task is not bound to its placement contract's cwd"
                )
            if cfg.desktop.project_id and thread.get("projectId") != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server production task lost its configured project association"
                )
            prompt = descriptor.prompt
            if str(session.get("kind") or "") == "pipeline_engineer":
                # The engineer's prompt is rebuilt here, not at reservation:
                # only here is a connection open, and the thread digest can
                # be taken from the server without leaving the working
                # directory or asking for an access Autopilot never answers.
                prompt = _pipeline_engineer_prompt_with_server_view(
                    cfg, production_client, session
                )
            turn_arguments = {
                "thread_id": thread_id,
                "prompt": prompt,
                "effort": descriptor.thinking if descriptor.thinking else None,
                "client_user_message_id": str(session["client_user_message_id"]),
                "cwd": expected_cwd,
                "workspace_roots": [workspace],
                "permission_profile": session_profile(cfg, session),
                "model": descriptor.model if descriptor.model else None,
            }
            if str(session.get("kind") or "") == "plan_verifier":
                # T5: no worker skill, planner transcript, or MCP context is
                # injected into the fresh plan-judgment session.
                started = production_client.start_plain_turn(**turn_arguments)
            else:
                started = production_client.start_turn(
                    **turn_arguments,
                    skill_name=cfg.skill_name,
                    skill_path=cfg.skill_path,
                )
            turn_id = str((started.get("turn") or {}).get("id") or "")
            if not turn_id:
                raise DesktopLifecycleError("App Server turn/start returned no turn id")
            acknowledge_desktop_send(
                cfg,
                reservation_token,
                thread_id=thread_id,
                relay_executor_thread_id=owner,
            )
            result = production_client.wait_for_turn(
                thread_id,
                turn_id,
                timeout=cfg.desktop.turn_timeout_seconds,
                pause_requested=StateStore(cfg.state_dir).pause_requested,
            )
            completed_turn = dict(result.turn)
            if not completed_turn.get("error") and result.errors:
                completed_turn["error"] = (
                    result.errors[-1].get("error") or result.errors[-1]
                )
        if connected_client is None and (
            client is None or not _client_process_exited(client)
        ):
            raise DesktopLifecycleError(
                "automatic App Server production process did not fully exit"
            )
    except PauseRequested:
        record_desktop_failure(
            cfg,
            reservation_token,
            reason="automatic App Server worker paused",
            failure_code="worker_paused",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id or None,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise
    except ApprovalRequired as exc:
        # Never answered, never retried: its own failure code, and a stop
        # ticket that holds the task and goes to the on-call (approval_stops).
        from .approval_stops import record_approval_required

        record_approval_required(
            cfg,
            reservation_token,
            exc.payload,
            thread_id=thread_id,
            turn_id=turn_id or None,
            owner=owner,
            now_epoch=now_epoch,
        )
        raise DesktopLifecycleError(str(exc)) from exc
    except Exception as exc:
        state = StateStore(cfg.state_dir).load()
        current = _session_by_token(state, reservation_token)
        if current.get("status") in {"SEND_RELAYING", "ACTIVE"}:
            rpc_method = exc.method if isinstance(exc, AppServerRpcError) else None
            limited = is_rate_limit_error(getattr(exc, "error", None))
            record_desktop_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                failure_code="app_server_rpc_failed",
                definitive=(rpc_method == "turn/start" or bool(turn_id)),
                thread_id=thread_id,
                turn_id=turn_id or None,
                rate_limited=limited,
                reset_at=_rate_limit_reset(client) if limited else None,
                now_epoch=now_epoch,
                reserve_other_ready=False,
                relay_executor_thread_id=owner,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    assert completed_turn is not None
    if completed_turn.get("status") == "completed":
        # R5 asks for the placement within N seconds of creation; a thread
        # with no turn is not persisted yet, so the first completed turn is
        # when the measurement is final. Never a stop (placement_defects).
        try:
            _require_thread_placement(
                cfg, reservation_token, client_factory=client_factory,
                connected_client=connected_client, at=None, phase="after_first_turn",
            )
        except Exception as exc:  # noqa: BLE001 - a measurement may not cost accepted work
            _append_placement_failure(cfg, reservation_token, exc)
    if completed_turn.get("status") != "completed":
        reason = json.dumps(
            completed_turn.get("error") or completed_turn,
            ensure_ascii=False,
            sort_keys=True,
        )
        limited = is_rate_limit_error(completed_turn.get("error"))
        record_desktop_failure(
            cfg,
            reservation_token,
            reason=f"App Server production turn ended non-completed: {reason}",
            failure_code="turn_ended_non_completed",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id,
            rate_limited=limited,
            reset_at=_rate_limit_reset(client) if limited else None,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise DesktopLifecycleError(reason)

    # The local dispatcher is authoritative. The worker Stop hook observes an
    # owned automatic turn but never consumes it or starts its successor.
    final_message = final_agent_message(completed_turn)
    if str(session.get("kind") or "") == "plan_verifier":
        allowed_items = {"agentMessage", "reasoning", "userMessage"}
        disallowed = sorted(
            {
                str(item.get("type") or "unknown")
                for item in completed_turn.get("items") or []
                if item.get("type") not in allowed_items
            }
        )
        if disallowed:
            # Feed an unreadable result to the bounded protocol-retry path.
            # Code that detects a boundary violation must not silently accept
            # or repair it and continue (R22).
            final_message = (
                "plan verifier used forbidden tool or side-effect items: "
                + ", ".join(disallowed)
            )
    try:
        return complete_desktop_worker(
            cfg,
            thread_id=thread_id,
            turn_id=turn_id,
            final_message=final_message,
            now_epoch=now_epoch,
            dispatcher_reservation_token=reservation_token,
            dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
        )
    except WorkerProtocolError as exc:
        # A malformed final reply is a model error, not a machine fault. It
        # used to propagate up: the dispatcher crashed, a PIPELINE ticket
        # opened, the engineer was raised, and the completed turn stayed
        # unaccepted - the whole run stood waiting for a human. R31 forbids
        # rejecting already done work at a late gate. Here it is an ordinary
        # failed attempt of the task: the reason is recorded, the task gets
        # a retry, the infrastructure is not involved.
        descriptors = record_desktop_failure(
            cfg,
            reservation_token,
            reason=f"worker protocol: {exc}",
            failure_code="worker_protocol_rejected",
            definitive=False,
            thread_id=thread_id,
            turn_id=turn_id,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        return CompletionOutcome(
            matched=True,
            worker_status="PROTOCOL_RETRY",
            descriptors=tuple(descriptors),
            run_done=False,
        )

def _notify_start(cfg: Config, descriptor: LaunchDescriptor) -> None:
    """Tell the human that the task has been taken up."""

    from .notify import notify

    notify(
        cfg,
        "Codex Autopilot",
        cfg.root.name,
        f"{descriptor.task_id} taken up: {descriptor.task_title}",
    )


def _rate_limit_reset(client: Any) -> int | None:
    """When the server itself says the limit will lift.

    Without this the pause after a limit was a guess: a generic retry
    interval unrelated to the real window. The functions that ask the server
    were written and never called - the barrier existed, and nobody fetched
    the data for it.

    A failure to ask is not a failure of work: the previous estimate stands.
    """

    from .appserver import rate_limit_reset_at

    try:
        return rate_limit_reset_at(client.rate_limits())
    except Exception:
        return None


def record_automatic_app_server_exit(
    cfg: Config,
    reservation_token: str,
    *,
    dispatcher_pid: int,
    at: str | None = None,
) -> None:
    """Journal the per-task App Server full-exit barrier.

    The dispatcher process may continue with a successor, but each task gets a
    fresh App Server subprocess. Recording the barrier before successor
    adoption proves the completed task has no surviving transport writer.
    """

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if session.get("automatic_dispatch_pid") != dispatcher_pid:
            raise DesktopLifecycleError(
                "App Server exit acknowledgement does not match dispatcher ownership"
            )
        if not session.get("completed_at"):
            raise DesktopLifecycleError(
                "App Server exit acknowledgement requires an authoritative completed turn"
            )
        if session.get("app_server_worker_exited_at"):
            return
        session["app_server_worker_exited_at"] = timestamp
        session["automatic_dispatch_connection_pid"] = None
        if session.get("automatic_dispatch_state") == "COMPLETED":
            session["automatic_dispatch_pid"] = None
        _append_event(
            state,
            "app_server_worker_process_exited",
            session,
            timestamp,
            detail="full process exit before successor adoption",
        )
        store.save(state)

def causal_predecessor(state: Any, owner: str) -> dict[str, Any] | None:
    """The owner's latest session whose turn has really completed.

    Checking the status "COMPLETED" alone cut off a legitimate predecessor:
    a task that returned PLAN_CHANGE_REQUEST completed its turn and recorded
    turn_completed, but its session stays in PLAN_CHANGE_REQUESTED. control
    accounted for this long ago; a second copy of the check lived here - and
    nobody could raise the planner reserved by such a task.
    """

    from .control import _turn_is_completed

    return next(
        (
            item
            for item in reversed(state.worker_sessions)
            if item.get("thread_id") == owner
            and item.get("turn_id")
            and (
                item.get("status") == "COMPLETED"
                or _turn_is_completed(state, owner, str(item["turn_id"]))
            )
        ),
        None,
    )


def adopt_automatic_dispatcher_successor(
    cfg: Config,
    *,
    completed_reservation_token: str,
    successor_reservation_token: str,
) -> tuple[str, str]:
    """Move the v0.7-style dispatcher loop to its exact reserved successor."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        completed = _session_by_token(state, completed_reservation_token)
        successor = _session_by_token(state, successor_reservation_token)
        if (
            completed.get("automatic_dispatch_state") != "ADVANCING"
            or completed.get("automatic_dispatch_pid") != os.getpid()
            or successor_reservation_token
            not in set(completed.get("automatic_successor_tokens") or [])
        ):
            raise DesktopLifecycleError(
                "current dispatcher does not own the completed-to-successor transition"
            )
        if successor.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
            raise DesktopLifecycleError("automatic successor is not launchable")
        owner = str(successor.get("relay_owner_thread_id") or "")
        if not owner:
            raise DesktopLifecycleError("automatic successor has no causal owner")
        predecessor = causal_predecessor(state, owner)
        if predecessor is None:
            raise DesktopLifecycleError(
                "automatic successor has no completed causal predecessor turn"
            )
        completed["automatic_dispatch_state"] = "COMPLETED"
        completed["automatic_dispatch_pid"] = None
        successor["automatic_dispatch_state"] = "RUNNING"
        successor["automatic_dispatch_pid"] = os.getpid()
        successor["automatic_dispatch_adopted_at"] = utc_now()
        store.save(state)
        return owner, str(predecessor["turn_id"])

def claim_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str,
    dispatcher_pid: int | None = None,
) -> LaunchDescriptor:
    """Claim the one production ``turn/start`` for the local dispatcher."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "PREPARED":
            raise DesktopLifecycleError(
                "automatic production turn was already claimed or creation is incomplete"
            )
        if session.get("creation_transport") != "app_server_thread_start":
            raise DesktopLifecycleError(
                "automatic production requires an App Server-created task"
            )
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        workspace = _descriptor_workspace(cfg, descriptor)
        if _thread_cwd({"cwd": session.get("actual_cwd")}) != session_cwd(cfg, session, workspace):
            raise DesktopLifecycleError(
                "automatic production requires the placement contract's cwd"
            )
        retained_connection = bool(
            _dispatcher_owns_reservation(session, dispatcher_pid=dispatcher_pid)
            and session.get("automatic_dispatch_connection_pid") == dispatcher_pid
        )
        creator_gone = _creator_process_is_gone(session)
        if (
            not session.get("app_server_create_exited_at")
            and not retained_connection
            and not creator_gone
        ):
            raise DesktopLifecycleError(
                "automatic production requires either the v0.7 dispatcher connection "
                "or the legacy creator process exit barrier"
            )
        if creator_gone and not session.get("app_server_create_exited_at"):
            # The barrier exists for one thing: to prove the creator no
            # longer writes into this thread. A dead process proves that no
            # worse than the normal mark it never got to set before crashing.
            session["app_server_create_exited_at"] = utc_now()
            _append_event(
                state,
                "app_server_create_exit_inferred",
                session,
                session["app_server_create_exited_at"],
                detail=(
                    "creator pid "
                    f"{session.get('automatic_dispatch_connection_pid')} is gone"
                ),
            )
        session["status"] = "SEND_RELAYING"
        timestamp = utc_now()
        _append_event(state, "automatic_turn_claimed", session, timestamp)
        _append_event(state, "start_requested", session, timestamp)
        state.phase = "AWAITING_APP_SERVER_TURN_ACK"
        store.save(state)
        return descriptor
