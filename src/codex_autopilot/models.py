from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MODEL_IDS = {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"}
MODEL_LABELS = {"sol": "GPT-5.6 Sol", "astra": "GPT-6 Astra"}
STRATEGIES = {"auto", "sol-only", "astra-only", "host-settings"}
EXECUTION_MODES = {"code", "computer_use"}
# The ladder stops at max ON PURPOSE. App Server offers one more rung
# above it - "ultra", described as "Maximum reasoning with automatic task
# delegation" - and it was measured on 21 Sep 2026 rather than reasoned
# about:
#
#   one thread/start sent; four threads on the wire. The server created
#   three of its own, each running its own turn and returning its own
#   9-12 KB answer. From the parent thread the turn reported a single
#   agentMessage item and nothing else - wait_for_turn would hand the
#   dispatcher a clean one-item result while three unrecorded threads had
#   just spent the user's limits.
#
# That is not a deeper worker, it is an orchestrator inside an
# orchestrator. Autopilot journals only the creations it performs, so
# those threads would exist in no journal; audit_creation_causality reads
# that journal and would keep printing "N/N creations audited" beside
# them - blind rather than broken, which is worse. The worker-slot
# accounting would not see them either.
#
# Adding "ultra" here is therefore not a missing rung. Reinstate it only
# with a way to observe and attribute the threads it creates.
PUBLIC_REASONING = ("medium", "high", "xhigh", "max")


class ModelRoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogVerdict:
    """What the live catalog says about one pinned model.

    ``state`` is "present", "missing" or "superseded". The last one is not
    a problem and never changes a run: a newer model of the same family
    exists and the owner has not moved to it. Saying so is the whole point
    - the runtime asks App Server for an exact id and refuses everything
    else, so the day that id is retired every installed copy stops at
    once. That day should not be the first time anyone hears about it.
    """

    key: str
    pinned: str
    state: str
    newer: str | None = None
    available: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.state == "missing":
            offer = ", ".join(self.available) or "nothing"
            return (
                f"{self.pinned} is no longer served to this account. "
                f"Available instead: {offer}. Nothing is substituted "
                f"automatically - choose one and say so."
            )
        if self.state == "superseded":
            return (
                f"{self.pinned} still works, and {self.newer} is newer. "
                f"Runs stay on {self.pinned} until you say otherwise."
            )
        return f"{self.pinned} is served."


def _family(model_id: str) -> str:
    """The trailing name a family keeps across versions: sol, astra."""

    return model_id.rsplit("-", 1)[-1].strip().lower()


def _version(model_id: str) -> tuple[int, ...]:
    """The numeric part of an id, for ordering within one family.

    Anything unparseable sorts lowest rather than raising: this feeds a
    message, never a decision, and an id shaped in a way nobody expected
    must not be able to stop a run.
    """

    import re

    found = re.findall(r"\d+(?:\.\d+)*", model_id)
    if not found:
        return (-1,)
    return tuple(int(part) for part in found[-1].split("."))


def catalog_verdicts(
    catalog: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    pinned: dict[str, str] | None = None,
) -> tuple[CatalogVerdict, ...]:
    """Compare what this account is served against what the runtime pins."""

    pinned = dict(pinned or MODEL_IDS)
    served = []
    for item in catalog or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("model") or item.get("id") or "").strip()
        if name:
            served.append(name)

    verdicts = []
    for key, model_id in pinned.items():
        family = _family(model_id)
        siblings = [name for name in served if _family(name) == family]
        if model_id not in served:
            verdicts.append(
                CatalogVerdict(key, model_id, "missing", available=tuple(siblings))
            )
            continue
        newer = [name for name in siblings if _version(name) > _version(model_id)]
        if newer:
            best = max(newer, key=_version)
            verdicts.append(CatalogVerdict(key, model_id, "superseded", newer=best))
            continue
        verdicts.append(CatalogVerdict(key, model_id, "present"))
    return tuple(verdicts)


@dataclass(frozen=True, slots=True)
class ModelSelection:
    key: str
    model_id: str
    display_name: str
    reasoning: str
    reason: str
    reasoning_adjustment: str | None = None


def next_effort_step(current: str | None) -> str | None:
    """The next effort step, or None when the ladder has run out.

    This is the way of reaching the result in hiring terms: the plan and
    DoD are untouchable, what changes is the executor and how much it
    thinks. The model is not a step: under the `auto` strategy it is tied
    hard to the task's execution_mode, and swapping the model would swap
    the declared capability, not the diligence.
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
        # R31: a refusal names what IS accepted. "Unavailable" alone left
        # the reader to guess whether the model was renamed, retired, or
        # never theirs.
        verdict = next(
            (item for item in catalog_verdicts(list(catalog), {key: expected})),
            None,
        )
        detail = verdict.message if verdict else f"{expected} is not served."
        raise ModelRoutingError(
            f"Required model {expected} is unavailable for this account; "
            f"no fallback was used. {detail}"
        )
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
