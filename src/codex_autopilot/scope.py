"""Правило R7: работа не выходит за объявленную область.

Область задачи объявляется через её ResourceClaim с файловыми kind
(path/directory/glob) и режимом доступа write или exclusive. Заявка с
доступом read областью записи не является: прочитать файл можно, менять
его - нет.

Задача, не объявившая ни одной файловой заявки на запись, не вправе
менять ничего. Это не формальность: в живом прогоне v0.9 у ВСЕХ задач
resources был пуст, поэтому выйти за область было невозможно по
построению, и воркеры правили что угодно. Пустая заявка должна давать
громкий дефект, а не молчаливое разрешение.

Фактически изменённые пути берутся из git. Если наблюдение недоступно
(проект не под git, git не установлен), аудит честно сообщает, что
проверка не проводилась, вместо того чтобы вернуть "нарушений нет".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import STATE_DIR_NAME
from .plan import Task
from .resources import (
    FILESYSTEM_RESOURCE_KINDS,
    NormalizedResourceClaim,
    _glob_matches,
    _is_within,
    normalize_task_claims,
)

WRITE_ACCESS_MODES = frozenset({"write", "exclusive"})

__all__ = [
    "WRITE_ACCESS_MODES",
    "ScopeNotObservable",
    "audit_declared_scope",
    "observe_changed_paths",
    "scope_baseline",
]


class ScopeNotObservable(Exception):
    """Изменённые пути наблюдать нечем; область не проверена."""


def scope_baseline(root: Path) -> str | None:
    """Ревизия на момент старта задачи, относительно которой считается диф."""

    head = _git(root, "rev-parse", "HEAD")
    return head.strip() if head else None


def observe_changed_paths(root: Path, baseline: str | None) -> tuple[str, ...]:
    """Абсолютные пути, изменённые с baseline, включая незакоммиченное.

    Собственное состояние рантайма (.codex-autopilot) исключается: эти
    файлы пишет не воркер, а сам автопилот - журнал, резервирования,
    файлы передачи. Без исключения любая задача нарушала бы R7 всегда,
    и правило превратилось бы в шум, который перестают читать.

    Поднимает ScopeNotObservable, если наблюдение невозможно - вызывающий
    обязан записать это как непроверенное, а не как чистый результат.
    """

    root = root.resolve(strict=False)
    if _git(root, "rev-parse", "--show-toplevel") is None:
        raise ScopeNotObservable(f"{root} is not a git work tree")
    # Имена берутся без разбора статус-префиксов: git отдаёт их как есть.
    # --no-renames оставляет обе стороны переименования, потому что для
    # области это два разных пути, а не один.
    against = baseline or "HEAD"
    changed = _git(root, "diff", "--name-only", "--no-renames", against)
    if changed is None:
        raise ScopeNotObservable(f"cannot diff against {against}")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if untracked is None:
        raise ScopeNotObservable("cannot list untracked files")
    names = set(changed.splitlines()) | set(untracked.splitlines())
    return tuple(
        sorted(
            str((root / name).resolve(strict=False))
            for name in names
            if name and not _is_runtime_state(name)
        )
    )


def _is_runtime_state(name: str) -> bool:
    head = Path(name).parts[:1]
    if not head:
        return False
    # Рантайм сам кладёт рядом свои архивы: `.codex-autopilot.stuck-<время>`
    # от --replace, снимки прежних прогонов. Имя у них другое, под точное
    # сравнение они не попадали - и собственный мусор рантайма предъявлялся
    # воркеру как запись вне объявленной области.
    #
    # Замерено: задача M0 заблокирована по R7 за 37 путей, все до одного
    # внутри .codex-autopilot.stuck-20260914T184420. Работы она там не
    # вела; каталог создал установщик прогона.
    return head[0] == STATE_DIR_NAME or head[0].startswith(STATE_DIR_NAME + ".")


def audit_declared_scope(
    task: Task,
    changed_paths: tuple[str, ...],
    *,
    project_root: Path,
) -> list[str]:
    """Пути вне объявленной области - дефект с указанием R7 и перечнем."""

    writable = [
        claim
        for claim in normalize_task_claims(task, project_root)
        if claim.kind in FILESYSTEM_RESOURCE_KINDS and claim.access in WRITE_ACCESS_MODES
    ]
    offenders = [
        path
        for path in changed_paths
        if not any(_covers(claim, path) for claim in writable)
    ]
    if not offenders:
        return []
    listed = ", ".join(offenders[:10])
    if len(offenders) > 10:
        listed += f" (и ещё {len(offenders) - 10})"
    if not writable:
        return [
            f"R7: задача {task.id} не объявила ни одной файловой заявки на запись, "
            f"но изменила {len(offenders)} путей: {listed}. "
            "Область задаётся ResourceClaim с kind path/directory/glob и access write"
        ]
    return [
        f"R7: задача {task.id} изменила {len(offenders)} путей вне объявленной "
        f"области: {listed}. Для работы вне области нужен PLAN_CHANGE_REQUEST"
    ]


def _covers(claim: NormalizedResourceClaim, path: str) -> bool:
    if claim.kind == "path":
        return claim.target == path
    if claim.kind == "directory":
        return _is_within(path, claim.target)
    return _glob_matches(claim.target, path)


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *args),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
