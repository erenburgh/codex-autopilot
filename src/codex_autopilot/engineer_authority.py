"""Полномочия дежурного инженера: что он вправе, а что запрещено.

Вынесено из pipeline_engineer.py отдельным модулем не ради порядка. С
тех пор как инженер получил право править код рантайма, всё лежавшее с
ним в одном файле пришлось бы запретить целиком - а в том же файле
живёт обычная бухгалтерия инцидентов, где настоящие дефекты и
случаются. Один такой мы чинили руками на прогоне v1.0: эскалация не
принималась после того, как инженер уже починил поломку.

Поэтому граница проходит здесь. Этот модуль правке не подлежит и
сверяется хэшами: класс поломки, словарь действий, список запретов и
порог, после которого способ починки начинает работать без человека.
Всё остальное в pipeline_engineer.py инженер чинить вправе - как чинил
бы любой другой модуль рантайма, через доказательство.
"""

from __future__ import annotations

from enum import Enum


class IncidentClass(str, Enum):
    PRODUCTION = "PRODUCTION"
    PIPELINE = "PIPELINE"
    RUNTIME = "RUNTIME"
    INTEGRATION = "INTEGRATION"
    TOOLING = "TOOLING"
    POLICY = "POLICY"
    AMBIGUOUS_SIDE_EFFECT = "AMBIGUOUS_SIDE_EFFECT"


class SideEffectOutcome(str, Enum):
    NONE = "NONE"
    KNOWN_FAILED = "KNOWN_FAILED"
    KNOWN_SUCCEEDED = "KNOWN_SUCCEEDED"
    UNKNOWN = "UNKNOWN"


# Операции, которые меняют состояние на той стороне. Отказ такой
# операции с неизвестным исходом - единственный случай, когда повтор
# запрещён вслепую: именно так в живом прогоне появлялись лишние ветки.
MUTATING_TRANSPORT_OPERATIONS = frozenset({"create_thread", "send_message_to_thread"})

# Классы, которыми занимается инженер пайплайна. Продакшен сюда не
# входит: качество продукта - работа воркеров, а не его.
INFRASTRUCTURE_INCIDENT_CLASSES = frozenset(
    {
        IncidentClass.PIPELINE,
        IncidentClass.RUNTIME,
        IncidentClass.INTEGRATION,
        IncidentClass.TOOLING,
    }
)

READ_ONLY_DIAGNOSTIC_ACTIONS = (
    "inspect_bounded_system_state",
    "inspect_recent_events",
    "reconcile_durable_journal",
    "run_declared_healthcheck",
)

# Действия, которые меняют состояние, а не только читают его. Каждое
# отвечает ровно одной команде восстановления, и у каждой из них свой
# отказ, когда предпосылки не выполнены. Называть их можно только так,
# как они называются: пересказ прозой не сходится ни с чем.
REPAIR_ACTIONS = (
    "rearm_relay_owner",
    "rearm_run",
    "reconcile_thread_identity",
    "recreate_archived_retry",
    "record_definitive_transport_failure",
    "record_completed_worker_turn",
    "repair_runtime_code",
)

# Весь словарь: чем инженер вправе отчитаться о починке.
RECOVERY_ACTIONS = READ_ONLY_DIAGNOSTIC_ACTIONS + REPAIR_ACTIONS

# Что уровень 1 вправе повторить сам, без человека. Диагностика - вся;
# из чинящих только те две команды, что сами отказывают, когда их
# предпосылки не выполнены, и потому безопасны при слепом повторе.
# Правка кода не повторяется никогда: патч, снявший поломку здесь, на
# другой машине и в другом состоянии - не лечение, а совпадение.
AUTO_REPLAYABLE_ACTIONS = READ_ONLY_DIAGNOSTIC_ACTIONS + (
    "rearm_relay_owner",
    "rearm_run",
)

FORBIDDEN_ACTIONS = (
    "fix_production_quality_failures",
    "bypass_trust_or_permission_checks",
    "impersonate_or_speak_for_the_user",
    "change_global_codex_settings",
    "authorize_project_root_mutation_on_behalf_of_the_user",
    "delete_project_state",
    "perform_destructive_or_unbounded_repairs",
    "repeat_ambiguous_create_thread_or_send_message_to_thread",
    "create_or_message_codex_tasks_without_real_user_authority_or_an_official_platform_capability",
)

# Сколько одинаковых успешных решений одной подписи нужно, чтобы способ
# перестал требовать инженера и стал детерминированным раннбуком.
PROMOTION_THRESHOLD = 2
