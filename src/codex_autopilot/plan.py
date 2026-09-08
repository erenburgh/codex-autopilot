from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import EXECUTION_MODES, STRATEGIES
from .reasoning import normalize


PLAN_FILE = "plan.json"


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    title: str
    objective: str
    definition_of_done: tuple[str, ...]
    execution_mode: str
    execution_mode_reason: str
    reasoning: str | None


@dataclass(frozen=True, slots=True)
class Plan:
    goal: str
    model_strategy: str
    milestones: tuple[Milestone, ...]


def validate_plan(data: dict[str, Any], profile: str) -> Plan:
    goal = str(data.get("goal", "")).strip()
    if not goal:
        raise ValueError("plan.goal must be non-empty")
    raw_milestones = data.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("plan.milestones must be a non-empty array")
    strategy = str(data.get("model_strategy") or ("auto" if profile == "adaptive" else "host-settings"))
    if strategy not in STRATEGIES:
        raise ValueError(f"model_strategy must be one of {sorted(STRATEGIES)}")
    if profile == "adaptive" and strategy == "host-settings":
        raise ValueError("Adaptive profile requires auto, sol-only, or astra-only model_strategy")
    if profile == "host-settings" and strategy != "host-settings":
        raise ValueError("Host Settings profile requires model_strategy=host-settings")
    milestones: list[Milestone] = []
    for index, raw in enumerate(raw_milestones, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"milestone {index} must be an object")
        title = str(raw.get("title", "")).strip()
        objective = str(raw.get("objective", "")).strip()
        done = raw.get("definition_of_done")
        if not title or not objective or not isinstance(done, list) or not done:
            raise ValueError(f"milestone {index} requires title, objective, and definition_of_done")
        done_items = tuple(str(item).strip() for item in done if str(item).strip())
        if not done_items:
            raise ValueError(f"milestone {index} has an empty definition_of_done")
        execution_mode = str(raw.get("execution_mode", "")).strip()
        execution_mode_reason = str(raw.get("execution_mode_reason", "")).strip()
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"milestone {index} execution_mode must be one of {sorted(EXECUTION_MODES)}")
        if not execution_mode_reason:
            raise ValueError(f"milestone {index} requires a concrete execution_mode_reason")
        reasoning = None
        if profile == "adaptive":
            raw_effort = raw.get("reasoning")
            if raw_effort is None:
                raise ValueError(f"milestone {index} requires reasoning in Adaptive profile")
            reasoning = normalize(str(raw_effort))
        elif "reasoning" in raw:
            raise ValueError("Host Settings plans must omit milestone reasoning")
        milestones.append(Milestone(f"M{index}", title, objective, done_items, execution_mode, execution_mode_reason, reasoning))
    return Plan(goal, strategy, tuple(milestones))


def load_plan(state_dir: Path, profile: str) -> Plan:
    data = json.loads((state_dir / PLAN_FILE).read_text(encoding="utf-8"))
    return validate_plan(data, profile)


def save_plan(state_dir: Path, plan: Plan) -> None:
    payload = {
        "schema_version": 2,
        "goal": plan.goal,
        "model_strategy": plan.model_strategy,
        "milestones": [
            {
                "id": item.id,
                "title": item.title,
                "objective": item.objective,
                "definition_of_done": list(item.definition_of_done),
                "execution_mode": item.execution_mode,
                "execution_mode_reason": item.execution_mode_reason,
                **({"reasoning": item.reasoning} if item.reasoning else {}),
            }
            for item in plan.milestones
        ],
    }
    atomic_json(state_dir / PLAN_FILE, payload)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
