from __future__ import annotations

import re


MAX_THREAD_TITLE_CHARS = 96


class ThreadTitleError(ValueError):
    pass


def implementation_thread_title(
    task_id: str,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    return _task_title(role_name, "Implement", task_id, task_title)


def verifier_thread_title(
    task_id: str,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    return _task_title(role_name, "Verify", task_id, task_title)


def revision_thread_title(
    task_id: str,
    revision_number: int,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    if isinstance(revision_number, bool) or not isinstance(revision_number, int) or revision_number <= 0:
        raise ThreadTitleError("revision_number must be a positive integer")
    return _task_title(
        role_name,
        "Revise",
        f"{_identifier(task_id, 'task_id')}-R{revision_number}",
        task_title,
    )


def planner_thread_title(goal_summary: str) -> str:
    return _compose("Plan", goal_summary)


def replanner_thread_title(plan_change_id: str, change_summary: str) -> str:
    return _compose(
        f"Replan {_identifier(plan_change_id, 'plan_change_id')}",
        change_summary,
    )


def task_phase_thread_title(
    *,
    task_id: str,
    task_title: str,
    kind: str,
    role_name: str,
    revision_number: int = 0,
) -> str:
    if kind in {"worker", "implementation"}:
        return implementation_thread_title(task_id, task_title, role_name=role_name)
    if kind == "verifier":
        return verifier_thread_title(task_id, task_title, role_name=role_name)
    if kind == "revision":
        return revision_thread_title(
            task_id,
            revision_number,
            task_title,
            role_name=role_name,
        )
    raise ThreadTitleError(f"unsupported thread phase kind: {kind!r}")


def _task_title(
    role_name: str | None,
    phase: str,
    task_id: str,
    task_title: str,
) -> str:
    """Render the stable phase/task title, with an optional human role prefix."""

    prefix = f"{phase} {_identifier(task_id, 'task_id')}"
    if role_name is not None:
        role = _text(role_name, "role_name")
        if role.casefold() != "legacy serial worker":
            prefix = f"{role} · {prefix}"
    return _compose(prefix, task_title)


def _compose(prefix: str, summary: str) -> str:
    clean_summary = _text(summary, "summary")
    value = f"{prefix} · {clean_summary}"
    if len(value) <= MAX_THREAD_TITLE_CHARS:
        return value
    available = MAX_THREAD_TITLE_CHARS - len(prefix) - 3
    if available < 2:
        raise ThreadTitleError("thread title prefix exceeds the title limit")
    return f"{prefix} · {clean_summary[: available - 1].rstrip()}…"


def _text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ThreadTitleError(f"{name} must be a string")
    result = re.sub(r"\s+", " ", value).strip()
    if not result:
        raise ThreadTitleError(f"{name} must be non-empty")
    return result


def _identifier(value: str, name: str) -> str:
    result = _text(value, name)
    if any(character in result for character in ("·", "\n", "\r")):
        raise ThreadTitleError(f"{name} contains a reserved title separator")
    return result
