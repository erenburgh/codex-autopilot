"""Every violation of a plan in one round: the collector and its exception.

The plan validator used to stop at its first ``raise ValueError``. A
replanner therefore learned its mistakes one per round, and it has three
rounds: a graph with four independent defects could not be fixed within the
budget even by a model that fixes everything it is told. Measured on the v1.0
run - the replanner wrote ``lead_role`` for ``lead_role_id``, was refused,
fixed it, and was refused for the next field it had never been shown.

``IssueCollector.check`` runs one independent condition and keeps its
``ValueError`` instead of letting it end the validation. Only ``ValueError``:
every plan validator raises one of its subclasses, and a ``TypeError`` or
``KeyError`` is a runtime defect, which must not be returned to a model as a
"plan issue" and so hidden (R3).

``FailFastCollector`` is the old behaviour under the same interface - the
legacy v0.8 path and any caller that wants the first error keep it.

With one violation the exception's text is that violation's text, so every
``except ValueError`` and every test matching a message still reads the same
words. With several it is ``plan has N issues:`` and a numbered list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class _Failed:
    """The value of a check that failed; never a valid plan value."""

    _instance: "_Failed | None" = None

    def __new__(cls) -> "_Failed":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "FAILED"

    def __bool__(self) -> bool:
        return False


FAILED: Any = _Failed()


class UnknownFieldsError(ValueError):
    """Unknown fields, carrying the accepted set so a refusal can name it (R31)."""

    def __init__(self, message: str, accepted: tuple[str, ...]) -> None:
        super().__init__(message)
        self.accepted = tuple(accepted)


@dataclass(frozen=True, slots=True)
class PlanIssue:
    stage: str
    path: str
    message: str
    accepted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "path": self.path,
            "message": self.message,
            **({"accepted": list(self.accepted)} if self.accepted else {}),
        }


def render_issues(issues: tuple[PlanIssue, ...] | list[PlanIssue]) -> str:
    items = list(issues)
    if len(items) == 1:
        return items[0].message
    return f"plan has {len(items)} issues:\n" + "\n".join(
        f"{index}. {item.message}" for index, item in enumerate(items, 1)
    )


class PlanIssues(ValueError):
    """All violations found in one pass, in a deterministic order."""

    def __init__(self, issues: tuple[PlanIssue, ...] | list[PlanIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(render_issues(self.issues))


class FailFastCollector:
    """The first violation ends the validation, exactly as before."""

    def check(self, stage: str, path: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    def add(self, stage: str, path: str, message: str, accepted: tuple[str, ...] = ()) -> None:
        raise UnknownFieldsError(message, accepted) if accepted else ValueError(message)

    def count(self) -> int:
        return 0

    def clean(self, *stages: str) -> bool:
        return True

    def raise_if_any(self) -> None:
        return None


FAIL_FAST = FailFastCollector()


class IssueCollector:
    """Keeps every violation; ``clean`` gates the checks that depend on a stage."""

    def __init__(self) -> None:
        self.issues: list[PlanIssue] = []

    def check(self, stage: str, path: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PlanIssues as exc:
            for item in exc.issues:
                self._keep(item)
            return FAILED
        except ValueError as exc:
            self._keep(PlanIssue(stage, path, str(exc), tuple(getattr(exc, "accepted", ()))))
            return FAILED

    def add(self, stage: str, path: str, message: str, accepted: tuple[str, ...] = ()) -> None:
        self._keep(PlanIssue(stage, path, message, tuple(accepted)))

    def _keep(self, issue: PlanIssue) -> None:
        # The same words twice say nothing new; a count would hide which.
        if not any(item.message == issue.message for item in self.issues):
            self.issues.append(issue)

    def count(self) -> int:
        return len(self.issues)

    def clean(self, *stages: str) -> bool:
        return not any(item.stage in stages for item in self.issues)

    def raise_if_any(self) -> None:
        if self.issues:
            raise PlanIssues(self.issues)
