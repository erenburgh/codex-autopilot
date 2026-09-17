from __future__ import annotations

import re


MAX_THREAD_TITLE_CHARS = 96
SEPARATOR = " | "

# M10-REF-002: the exact forms from the original v0.9 request, sections 27-30.
#
#   implementation   <Role> | <Task ID> | <Short Task Title>
#   verifier         <Role> Verifier | <Task ID> | Verify <Subject>
#   revision         <Role> | <Task ID>-R<n> | Revise <Subject>
#   planner          Planner | PLAN | <Short Project Goal>
#   replanner        Planner | PC-<ID> | <Short Change Purpose>
#
# The previous implementation rendered "Role · Implement T44 · Title" and
# documented its format in docs/DESKTOP_RUNTIME.md as the norm. The
# documentation agreed with the code, and both disagreed with the request.

LEGACY_ROLE_NAMES = {"legacy serial worker", "legacy-worker"}

# The leading verb of a task statement, replaced by Verify/Revise:
# "Create Weapon Model" -> "Verify Weapon Model".
_ACTION_VERBS = {
    "add", "apply", "build", "configure", "connect", "create", "delete",
    "design", "disable", "document", "draft", "enable", "extend", "fix",
    "guarantee", "harden", "implement", "introduce", "make", "merge",
    "migrate", "normalize", "prepare", "refactor", "remove", "rename",
    "render", "replace", "restore", "run", "ship", "specify", "split",
    "stabilize", "update", "wire", "write",
    "внедрить", "восстановить", "добавить", "настроить", "написать",
    "перенести", "подготовить", "починить", "сделать", "собрать",
    "создать", "спроектировать", "стабилизировать", "удалить",
}


class ThreadTitleError(ValueError):
    pass


def implementation_thread_title(
    task_id: str,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    return _compose(
        _role_segment(role_name),
        _identifier(task_id, "task_id"),
        _text(task_title, "task_title"),
    )


def verifier_thread_title(
    task_id: str,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    return _compose(
        _verifier_role_segment(role_name),
        _identifier(task_id, "task_id"),
        _purpose(task_title, "Verify"),
    )


def department_verifier_thread_title(
    task_id: str,
    task_title: str,
    *,
    lead_role_name: str,
) -> str:
    """R30 title: <Lead Role> | Verify <Task ID> | <Short Task Title>."""

    return _compose(
        _role_segment(lead_role_name),
        f"Verify {_identifier(task_id, 'task_id')}",
        _text(task_title, "task_title"),
    )


def revision_thread_title(
    task_id: str,
    revision_number: int,
    task_title: str,
    *,
    role_name: str | None = None,
) -> str:
    if (
        isinstance(revision_number, bool)
        or not isinstance(revision_number, int)
        or revision_number <= 0
    ):
        raise ThreadTitleError("revision_number must be a positive integer")
    return _compose(
        _role_segment(role_name),
        f"{_identifier(task_id, 'task_id')}-R{revision_number}",
        _purpose(task_title, "Revise"),
    )


def replanner_thread_title(plan_change_id: str, change_summary: str) -> str:
    identifier = _identifier(plan_change_id, "plan_change_id")
    # Normalize to the exact PC-<ID> form: "PC-04", "PC1" and "04" yield
    # "PC-04" / "PC-1" / "PC-04" respectively.
    suffix = re.sub(r"^PC[-_ ]?", "", identifier, flags=re.IGNORECASE).strip()
    if not suffix:
        raise ThreadTitleError("plan_change_id must carry an identifier after PC")
    identifier = f"PC-{suffix}"
    return _compose("Planner", identifier, _text(change_summary, "change_summary"))


def plan_verifier_thread_title(graph_version: int, mode: str) -> str:
    if isinstance(graph_version, bool) or not isinstance(graph_version, int) or graph_version < 1:
        raise ThreadTitleError("graph_version must be a positive integer")
    summary = (
        "Full Plan Revalidation"
        if mode == "FULL_PLAN_REVALIDATION"
        else "Verify Proposed Plan Patch"
    )
    return _compose(
        "Plan Verification Architect",
        f"Verify PLAN-v{graph_version}",
        summary,
    )


def pipeline_engineer_thread_title(incident_id: str, summary: str) -> str:
    """The on-call engineer's thread title.

    The identifier is normalized to INC-<tail>: tickets arrive as
    incident-8ea3ceca87b6c8a3, and the sidebar needs a short recognizable
    prefix, not a raw store identifier.
    """

    identifier = _identifier(incident_id, "incident_id")
    suffix = re.sub(r"^(incident|INC)[-_ ]?", "", identifier, flags=re.IGNORECASE).strip()
    if not suffix:
        raise ThreadTitleError("incident_id must carry an identifier after the prefix")
    return _compose("Pipeline Engineer", f"INC-{suffix[:12]}", _text(summary, "summary"))


def task_phase_thread_title(
    *,
    task_id: str,
    task_title: str,
    kind: str,
    role_name: str,
    revision_number: int = 0,
    departmental_verifier: bool = False,
) -> str:
    if kind in {"worker", "implementation"}:
        return implementation_thread_title(task_id, task_title, role_name=role_name)
    if kind == "verifier":
        if departmental_verifier:
            return department_verifier_thread_title(
                task_id,
                task_title,
                lead_role_name=role_name,
            )
        return verifier_thread_title(task_id, task_title, role_name=role_name)
    if kind == "revision":
        return revision_thread_title(
            task_id,
            revision_number,
            task_title,
            role_name=role_name,
        )
    raise ThreadTitleError(f"unsupported thread phase kind: {kind!r}")


def _role_segment(role_name: str | None) -> str:
    """The role is mandatory: a phase title without a role is forbidden (rule R9).

    The generic legacy role stays allowed and renders as is - it is
    legitimate for a genuinely role-less schema-2 plan. The ban on
    collapsing a CONCRETE role into legacy-worker is a plan invariant, and
    plan validation must check it, not the title renderer.
    """
    if role_name is None:
        raise ThreadTitleError("role_name is required: a phase-only title is not allowed")
    return _identifier(role_name, "role_name")


def _verifier_role_segment(role_name: str | None) -> str:
    role = _role_segment(role_name)
    if role.casefold().endswith("verifier"):
        return role
    return f"{role} Verifier"


def _purpose(task_title: str, verb: str) -> str:
    """Derive the acceptance subject from the task statement.

    "Create Weapon Model" -> "Verify Weapon Model".

    A heuristic: the list of statement verbs cannot be complete. If the
    first word is not recognized, the verb is prepended, giving a longer
    but correct wording. Meaning cannot be lost this way - only brevity.
    """
    subject = _text(task_title, "task_title")
    head, _, tail = subject.partition(" ")
    if tail and head.casefold().strip(":,") in _ACTION_VERBS:
        subject = tail.strip()
    return f"{verb} {subject}"


def _compose(role: str, identifier: str, summary: str) -> str:
    prefix = f"{role}{SEPARATOR}{identifier}{SEPARATOR}"
    value = f"{prefix}{summary}"
    if len(value) <= MAX_THREAD_TITLE_CHARS:
        return value
    available = MAX_THREAD_TITLE_CHARS - len(prefix) - 1
    if available < 2:
        raise ThreadTitleError("thread title prefix exceeds the title limit")
    return f"{prefix}{summary[:available].rstrip()}…"


def _text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ThreadTitleError(f"{name} must be a string")
    result = re.sub(r"\s+", " ", value).strip()
    if not result:
        raise ThreadTitleError(f"{name} must be non-empty")
    return result


def _identifier(value: str, name: str) -> str:
    result = _text(value, name)
    if any(character in result for character in ("|", "\n", "\r")):
        raise ThreadTitleError(f"{name} contains a reserved title separator")
    return result
