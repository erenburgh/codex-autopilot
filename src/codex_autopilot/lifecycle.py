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

Реэкспортируется ровно то, что через фасад действительно импортируют, и
список закреплён в __all__. Механическое разрезание монолита протащило
сюда 91 имя, из них 47 приватных: приватный помощник публичным API не
был никогда, и его присутствие здесь делало границу неотличимой от
содержимого.
"""

from __future__ import annotations

from .lifecycle_base import (  # noqa: F401
    DESKTOP_SLOT_READY,
    DesktopLifecycleError,
    LaunchDescriptor,
    WORKSPACE_HANDOFF_OK,
    acknowledge_desktop_send,
    audit_creation_causality,
    creation_causality_coverage,
    parse_applied_rules,
    parse_desktop_worker_status,
    retired_session_for_thread,
    pause_desktop_run,
    pending_descriptors,
    observe_worker_states,
    reconcile_desktop_runtime,
    relay_session_status,
    task_checkpoint_path,
)

from .lifecycle_reservations import (  # noqa: F401
    relayable_descriptors,
    reserve_ready_frontier,
)

from .lifecycle_failures import (  # noqa: F401
    reconcile_desktop_thread_identity,
    record_desktop_failure,
    record_desktop_interrupt,
    record_policy_rejected_create_transport,
)

from .lifecycle_dispatch import (  # noqa: F401
    adopt_automatic_dispatcher_successor,
    claim_automatic_app_server_turn,
    create_desktop_thread_via_app_server,
    record_automatic_app_server_exit,
    run_automatic_app_server_turn,
)

from .lifecycle_completion import (  # noqa: F401
    complete_desktop_worker,
)

__all__ = [
    "DESKTOP_SLOT_READY",
    "DesktopLifecycleError",
    "LaunchDescriptor",
    "WORKSPACE_HANDOFF_OK",
    "acknowledge_desktop_send",
    "adopt_automatic_dispatcher_successor",
    "audit_creation_causality",
    "claim_automatic_app_server_turn",
    "complete_desktop_worker",
    "create_desktop_thread_via_app_server",
    "creation_causality_coverage",
    "parse_applied_rules",
    "parse_desktop_worker_status",
    "retired_session_for_thread",
    "pause_desktop_run",
    "pending_descriptors",
    "observe_worker_states",
    "reconcile_desktop_runtime",
    "reconcile_desktop_thread_identity",
    "record_automatic_app_server_exit",
    "record_desktop_failure",
    "record_desktop_interrupt",
    "record_policy_rejected_create_transport",
    "relay_session_status",
    "relayable_descriptors",
    "reserve_ready_frontier",
    "run_automatic_app_server_turn",
    "task_checkpoint_path",
]
