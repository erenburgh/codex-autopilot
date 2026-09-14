from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MODEL_IDS = {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"}
MODEL_LABELS = {"sol": "GPT-5.6 Sol", "astra": "GPT-6 Astra"}
STRATEGIES = {"auto", "sol-only", "astra-only", "host-settings"}
EXECUTION_MODES = {"code", "computer_use"}
PUBLIC_REASONING = ("medium", "high", "xhigh", "max")


class ModelRoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelSelection:
    key: str
    model_id: str
    display_name: str
    reasoning: str
    reason: str
    reasoning_adjustment: str | None = None


def next_effort_step(current: str | None) -> str | None:
    """Следующая ступень усилия, или None когда лестница кончилась.

    Это и есть способ достижения результата в терминах найма: план и DoD
    неприкосновенны, меняется исполнитель и то, сколько он думает. Модель
    ступенью не является: в стратегии `auto` она жёстко связана с
    execution_mode задачи, и подмена модели означала бы подмену заявленной
    способности, а не усердия.
    """
    ladder = PUBLIC_REASONING
    value = current or ladder[0]
    if value not in ladder:
        raise ModelRoutingError(f"reasoning must be one of {ladder}")
    index = ladder.index(value)
    if index + 1 >= len(ladder):
        return None
    return ladder[index + 1]


def logical_model(strategy: str, execution_mode: str) -> str:
    if strategy not in STRATEGIES - {"host-settings"}:
        raise ModelRoutingError(f"unsupported model strategy: {strategy}")
    if execution_mode not in EXECUTION_MODES:
        raise ModelRoutingError(f"unsupported execution mode: {execution_mode}")
    if strategy == "sol-only":
        if execution_mode == "computer_use":
            raise ModelRoutingError("This milestone requires Computer Use, but the run is configured as Sol-only.")
        return "sol"
    if strategy == "astra-only":
        return "astra"
    return "astra" if execution_mode == "computer_use" else "sol"


def resolve_selection(
    catalog: list[dict[str, Any]],
    *,
    strategy: str,
    execution_mode: str,
    requested_reasoning: str,
    execution_reason: str,
) -> ModelSelection:
    key = logical_model(strategy, execution_mode)
    expected = MODEL_IDS[key]
    model = next((item for item in catalog if item.get("id") == expected or item.get("model") == expected), None)
    if model is None:
        raise ModelRoutingError(f"Required model {expected} is unavailable for this account; no fallback was used.")
    actual = str(model.get("model") or model.get("id") or "")
    if actual != expected:
        raise ModelRoutingError(f"Model registry expected {expected}, but App Server advertised {actual or 'an empty id'}.")
    supported = tuple(
        str(item.get("reasoningEffort"))
        for item in model.get("supportedReasoningEfforts") or []
        if isinstance(item, dict) and item.get("reasoningEffort") in PUBLIC_REASONING
    )
    reasoning, adjustment = resolve_reasoning(requested_reasoning, supported)
    if strategy == "auto":
        route = "Computer Use is required" if key == "astra" else "Computer Use is not required"
        reason = f"AUTO: {route}. {execution_reason}".strip()
    elif strategy == "sol-only":
        reason = f"Sol-only strategy. {execution_reason}".strip()
    else:
        reason = f"Astra-only strategy. {execution_reason}".strip()
    return ModelSelection(key, expected, MODEL_LABELS[key], reasoning, reason, adjustment)


def resolve_reasoning(requested: str, supported: tuple[str, ...]) -> tuple[str, str | None]:
    if requested not in PUBLIC_REASONING:
        raise ModelRoutingError(f"reasoning must be one of {PUBLIC_REASONING}")
    available = tuple(item for item in PUBLIC_REASONING if item in supported)
    if not available:
        raise ModelRoutingError("Selected model advertises none of the public Adaptive reasoning efforts.")
    if requested in available:
        return requested, None
    target = PUBLIC_REASONING.index(requested)
    resolved = min(available, key=lambda value: (abs(PUBLIC_REASONING.index(value) - target), PUBLIC_REASONING.index(value)))
    return resolved, f"requested {requested}; App Server does not support it for this model, resolved to nearest supported {resolved}"
