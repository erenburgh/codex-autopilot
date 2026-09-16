from __future__ import annotations

from typing import Any


def topological_order(plan: Any) -> tuple[str, ...]:
    """Return a stable dependency order, using declaration order for ties."""

    rank = {task.id: index for index, task in enumerate(plan.tasks)}
    indegree = {task.id: len(task.depends_on) for task in plan.tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)
    ready = sorted(
        (task_id for task_id, count in indegree.items() if count == 0),
        key=rank.get,
    )
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for dependent in sorted(dependents[current], key=rank.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=rank.get)
    if len(result) != len(plan.tasks):
        raise ValueError("plan task graph contains a cycle")
    return tuple(result)
