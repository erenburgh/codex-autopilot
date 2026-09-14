"""Сколько воркеров можно запускать при текущем состоянии лимитов.

Заявленное пользователем число - его решение и потолок. Здесь оно может
только понижаться, и только когда лимит действительно рядом. Сказать
человеку с автосписанием "тебе положено десять" было бы наглостью:
он платит по факту, и ограничивать его нам не за что.

Данные приходят от App Server событием `account/rateLimits/updated` и
читаются по запросу через `account/rateLimits/read`:

    primary.usedPercent      сколько окна израсходовано
    primary.windowDurationMins  длина окна
    credits.unlimited        безлимит
    credits.hasCredits       есть кредиты, списание продолжится
    spendControlReached      пользователь сам поставил предел и достиг его
    rateLimitReachedType     лимит уже упёрт
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    """Решение о ёмкости вместе с его причиной.

    `workers = None` означает отсутствие потолка: на безлимитном
    аккаунте ограничивать нечем, и число одновременных воркеров задаёт
    сам граф - столько, сколько задач готово к работе.
    """

    workers: int | None
    reason: str
    limited: bool


def _snapshot(limits: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(limits, Mapping):
        return {}
    inner = limits.get("rateLimits")
    return inner if isinstance(inner, Mapping) else limits


def worker_budget(
    declared: int,
    limits: Mapping[str, Any] | None,
    *,
    declared_by_user: bool = False,
) -> WorkerBudget:
    """Сколько воркеров запускать сейчас и почему именно столько.

    `declared_by_user` означает, что число названо человеком явно. Такое
    число не повышается никогда - даже на безлимите: если он попросил
    три, значит три.
    """

    declared = max(1, int(declared))
    snapshot = _snapshot(limits)
    if not snapshot:
        # Нет данных - нет и повода урезать. Молчаливое понижение по
        # незнанию было бы худшим из вариантов: пользователь не поймёт,
        # почему прогон идёт медленнее, чем он попросил.
        return WorkerBudget(declared, "данных о лимитах нет", False)

    credits = snapshot.get("credits")
    credits = credits if isinstance(credits, Mapping) else {}
    if credits.get("unlimited") and not declared_by_user:
        # Потолка нет: сколько задач граф откроет одновременно, столько и
        # пойдёт. Навязывать здесь число значило бы ограничивать того,
        # кто платит по факту.
        return WorkerBudget(None, "безлимитный аккаунт: потолка нет", False)
    if credits.get("unlimited"):
        return WorkerBudget(declared, "безлимитный аккаунт, число задал пользователь", False)

    if snapshot.get("spendControlReached"):
        return WorkerBudget(1, "достигнут предел расходов, заданный пользователем", True)
    if snapshot.get("rateLimitReachedType"):
        return WorkerBudget(1, "лимит уже упёрт", True)

    primary = snapshot.get("primary")
    primary = primary if isinstance(primary, Mapping) else {}
    used = primary.get("usedPercent")
    if not isinstance(used, (int, float)):
        return WorkerBudget(declared, "расход окна неизвестен", False)

    remaining = max(0.0, 100.0 - float(used))
    # Кредиты смягчают: списание продолжится за окном, поэтому запас
    # считается на одну ступень щедрее.
    if credits.get("hasCredits"):
        remaining = min(100.0, remaining + 25.0)

    if remaining >= 50:
        return WorkerBudget(declared, f"израсходовано {used:.0f}% окна", False)
    if remaining >= 25:
        workers = max(2, declared // 2)
        return WorkerBudget(min(declared, workers), f"израсходовано {used:.0f}% окна", True)
    if remaining >= 10:
        return WorkerBudget(min(declared, 2), f"израсходовано {used:.0f}% окна", True)
    return WorkerBudget(1, f"израсходовано {used:.0f}% окна", True)


def capacity_notice(limits: Mapping[str, Any] | None, declared: int | None) -> str:
    """Что сказать человеку про ёмкость перед стартом прогона.

    Пользователь не обязан знать ни своего тарифа, ни того, что число
    воркеров вообще можно задать. Спросить его один раз, назвав его
    собственное положение, честнее, чем молча поставить десятку из
    шаблона - именно так она и простояла весь прогон на 24 задачи.
    """

    snapshot = _snapshot(limits)
    credits = snapshot.get("credits")
    credits = credits if isinstance(credits, Mapping) else {}
    plan_type = str(snapshot.get("planType") or "").strip()

    if declared is not None:
        return (
            f"Параллельных воркеров: {declared} - как вы указали. "
            "Изменить можно в любой момент, сказав другое число."
        )
    if credits.get("unlimited"):
        return (
            "У вас безлимитный аккаунт, поэтому потолка параллельных воркеров нет: "
            "одновременно пойдёт столько задач, сколько откроет план. "
            "Если хотите ограничить - скажите число."
        )
    if credits.get("hasCredits"):
        return (
            "У вас подключено списание кредитов, потолок параллельных воркеров - 10. "
            "Можно больше или меньше: скажите число."
        )
    if plan_type:
        return (
            f"Тариф {plan_type}: по умолчанию 10 параллельных воркеров, "
            "и они сами сузятся, когда окно лимита будет подходить к концу. "
            "Можно задать своё число."
        )
    return (
        "По умолчанию 10 параллельных воркеров. Можно задать своё число; "
        "при подходе к лимиту они сузятся сами."
    )
