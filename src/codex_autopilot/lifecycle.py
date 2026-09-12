"""Desktop-owned жизненный цикл: фасад над модулями реализации.

Модуль был единым файлом на 4844 строки. Разрезан по связности,
граф зависимостей односторонний:

    lifecycle_base          типы, журнал сессий, чекпойнты, мелкие помощники
      <- lifecycle_reservations   резервация фронтира и построение дескрипторов
      <- lifecycle_failures       отказы, инциденты, реконсиляция identity
      <- lifecycle_dispatch       создание задачи и production-ход через App Server
      <- lifecycle_completion     потребление авторитетного завершения
         lifecycle_prompts        сборка промптов фаз

Два обратных ребра развязаны поздними импортами внутри функций:
lifecycle_failures -> app_server_creation_contract и
lifecycle_dispatch -> complete_desktop_worker.

Этот файл ничего не реализует. Он существует, чтобы публичный API
остался прежним: и CLI, и тесты по-прежнему импортируют из
codex_autopilot.lifecycle.
"""

from __future__ import annotations

from .lifecycle_base import (  # noqa: F401
    audit_creation_causality,
    creation_causality_coverage,
    pending_descriptors,
    reconcile_desktop_runtime,
    ALLOWED_STATUSES,
    CompletionOutcome,
    DESCRIPTOR_SCHEMA_VERSION,
    DESKTOP_SLOT_READY,
    DesktopLifecycleError,
    DesktopSlotHistoryError,
    IMPLEMENTATION_SESSION_KINDS,
    LaunchDescriptor,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    WORKSPACE_HANDOFF_OK,
    WORKSPACE_HANDOFF_PROMPT,
    _active_session_by_thread,
    _append_event,
    _bind_resource_identity,
    _block_if_revision_limit_reached,
    _checkpoint,
    _checkpoint_slug,
    _client_process_exited,
    _client_process_pid,
    _contains_exact_text,
    _dispatcher_owns_reservation,
    _finish_global_state,
    _latest_completion_context,
    _latest_implementation_thread_id,
    _latest_task_session,
    _latest_verification_issues,
    _materialize,
    _pid_alive,
    _process_id_alive,
    _record_deterministic_evidence,
    _record_deterministic_verification_results,
    _require_desktop_owned,
    _require_relay_executor,
    _session_by_token,
    _session_kind,
    _stable_id,
    _stable_text_id,
    _sync_legacy_cursor,
    _synthetic_session,
    _text_matches_expected,
    _thread_cwd,
    _verified_prefix,
    _wait_for_dispatcher_ownership,
    acknowledge_desktop_send,
    confirm_prep_app_server_exit,
    fence_superseded_sessions,
    parse_desktop_worker_status,
    pause_desktop_run,
    relay_session_status,
    relay_success_report,
    session_is_fenced,
    task_checkpoint,
    task_checkpoint_path,
)
from .lifecycle_reservations import (  # noqa: F401
    ALLOWED_STATUSES,
    DESCRIPTOR_SCHEMA_VERSION,
    DESKTOP_SLOT_READY,
    IMPLEMENTATION_SESSION_KINDS,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    WORKSPACE_HANDOFF_OK,
    WORKSPACE_HANDOFF_PROMPT,
    _build_descriptor,
    _legacy_retry_recovery_context,
    _legacy_retry_requires_bound_owner,
    _prepare_state,
    _reserve_followup_sessions_in_state,
    _reserve_in_state,
    _reserve_replanner_in_state,
    recover_desktop_frontier_from_predecessor_stop,
    relayable_descriptors,
    reserve_ready_frontier,
)
from .lifecycle_failures import (  # noqa: F401
    ALLOWED_STATUSES,
    DESCRIPTOR_SCHEMA_VERSION,
    DESKTOP_SLOT_READY,
    IMPLEMENTATION_SESSION_KINDS,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    WORKSPACE_HANDOFF_OK,
    WORKSPACE_HANDOFF_PROMPT,
    _record_app_server_create_failure,
    _record_created_app_server_ambiguity,
    reconcile_desktop_thread_identity,
    record_desktop_failure,
    record_desktop_interrupt,
    record_policy_rejected_create_transport,
)
from .lifecycle_dispatch import (  # noqa: F401
    ALLOWED_STATUSES,
    DESCRIPTOR_SCHEMA_VERSION,
    DESKTOP_SLOT_READY,
    IMPLEMENTATION_SESSION_KINDS,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    WORKSPACE_HANDOFF_OK,
    WORKSPACE_HANDOFF_PROMPT,
    adopt_automatic_dispatcher_successor,
    app_server_creation_contract,
    claim_automatic_app_server_turn,
    create_desktop_thread_via_app_server,
    record_automatic_app_server_exit,
    run_automatic_app_server_turn,
)
from .lifecycle_completion import (  # noqa: F401
    ALLOWED_STATUSES,
    DESCRIPTOR_SCHEMA_VERSION,
    DESKTOP_SLOT_READY,
    IMPLEMENTATION_SESSION_KINDS,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    WORKSPACE_HANDOFF_OK,
    WORKSPACE_HANDOFF_PROMPT,
    _complete_replanner,
    complete_desktop_worker,
)
from .lifecycle_prompts import (  # noqa: F401
    _evidence_selectors,
    _replanner_prompt,
    _revision_prompt,
    _verification_contract,
    _verifier_prompt,
    _worker_prompt,
)
