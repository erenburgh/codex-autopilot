"""Typed task acceptance classes.

The class describes how much of a task's acceptance contract can be decided
mechanically.  It does not choose the verification policy and, in particular,
does not authorize a task to accept itself.  Under the canonical R29 floor,
deterministic checks are evidence admitted to fresh independent judgement for
all three classes.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class AcceptanceClass(str, Enum):
    """How mechanical and semantic acceptance contribute to a task."""

    DETERMINISTIC_COMPLETE = "deterministic-complete"
    MIXED = "mixed"
    JUDGMENT = "judgment"


ACCEPTANCE_CLASSES = frozenset(item.value for item in AcceptanceClass)
DEFAULT_ACCEPTANCE_CLASS = AcceptanceClass.MIXED


class AcceptanceClassError(ValueError):
    """The task's declared acceptance class is absent or malformed."""


def acceptance_class_from_raw(
    raw: Any,
    label: str,
    *,
    default: AcceptanceClass | None = DEFAULT_ACCEPTANCE_CLASS,
) -> AcceptanceClass:
    """Parse one plan declaration without inferring it from task prose.

    ``default`` exists for persisted schema-3 plans written before acceptance
    classes became first-class.  Callers may pass ``None`` when an explicit
    declaration is required.  Unknown values fail closed instead of silently
    becoming ``mixed``.
    """

    if raw is None:
        if default is None:
            raise AcceptanceClassError(
                f"{label} must be declared and be one of {sorted(ACCEPTANCE_CLASSES)}"
            )
        return default
    if not isinstance(raw, str):
        raise AcceptanceClassError(
            f"{label} must be one of {sorted(ACCEPTANCE_CLASSES)}"
        )
    value = raw.strip()
    try:
        return AcceptanceClass(value)
    except ValueError as exc:
        raise AcceptanceClassError(
            f"{label} must be one of {sorted(ACCEPTANCE_CLASSES)}; got {value!r}"
        ) from exc


def admits_semantic_judgment(acceptance_class: AcceptanceClass | str) -> bool:
    """Whether green mechanical checks still leave purpose to be judged.

    The return value describes the acceptance mechanism, not the R29 gate.
    A ``deterministic-complete`` task still needs the canonical independent
    verifier to attest that its contract really is complete and that the
    evidence belongs to the proposed result.
    """

    resolved = (
        acceptance_class
        if isinstance(acceptance_class, AcceptanceClass)
        else acceptance_class_from_raw(
            acceptance_class,
            "acceptance_class",
            default=None,
        )
    )
    return resolved in {AcceptanceClass.MIXED, AcceptanceClass.JUDGMENT}
