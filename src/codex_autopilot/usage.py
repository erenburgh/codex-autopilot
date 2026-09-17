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
        return WorkerBudget(declared, "no rate-limit data", False)

    credits = snapshot.get("credits")
    credits = credits if isinstance(credits, Mapping) else {}
    # Предел, заданный самим человеком, сильнее любых кредитов: он его и
    # ставил, чтобы списание остановилось. Прежде эта проверка стояла
    # ПОСЛЕ кредитов и потому не срабатывала вовсе у тех, ради кого была
    # написана - у аккаунтов с автосписанием.
    if snapshot.get("spendControlReached"):
        return WorkerBudget(1, "the spending cap set by the user has been reached", True)
    if snapshot.get("rateLimitReachedType"):
        return WorkerBudget(1, "the limit is already exhausted", True)

    if _burns_without_a_wall(credits):
        # Автосписание и есть безлимит: окно лимита такому аккаунту не
        # стена, списание идёт дальше. Потолка нет - сколько задач граф
        # откроет одновременно, столько и пойдёт.
        if declared_by_user:
            return WorkerBudget(declared, "unlimited billing, the number was set by the user", False)
        return WorkerBudget(None, "unlimited billing: no ceiling", False)

    primary = snapshot.get("primary")
    primary = primary if isinstance(primary, Mapping) else {}
    used = primary.get("usedPercent")
    if not isinstance(used, (int, float)):
        return WorkerBudget(declared, "window usage unknown", False)

    remaining = max(0.0, 100.0 - float(used))
    if remaining >= 50:
        return WorkerBudget(declared, f"{used:.0f}% of the window used", False)
    if remaining >= 25:
        workers = max(2, declared // 2)
        return WorkerBudget(min(declared, workers), f"{used:.0f}% of the window used", True)
    if remaining >= 10:
        return WorkerBudget(min(declared, 2), f"{used:.0f}% of the window used", True)
    return WorkerBudget(1, f"{used:.0f}% of the window used", True)


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
    plan_type = _human_plan_name(snapshot.get("planType"))

    if declared is not None:
        return (
            f"Parallel workers: {declared} - as you specified. "
            "You can change it at any time by naming another number."
        )
    if _burns_without_a_wall(credits):
        return (
            "Your billing is unlimited, so there is no ceiling on parallel workers: "
            "as many tasks run at once as the plan opens. "
            "To cap it, name a number."
        )
    fallback = default_workers(limits)
    if _is_plus(snapshot.get("planType")):
        return (
            f"Plan {plan_type}: {fallback} parallel workers by default - "
            "the limit window is narrow here, and ten would burn it in one run. "
            "You can set your own number."
        )
    if plan_type:
        return (
            f"Plan {plan_type}: {fallback} parallel workers by default, "
            "and they narrow by themselves as the limit window runs low. "
            "You can set your own number."
        )
    return (
        f"{fallback} parallel workers by default. You can set your own "
        "number; as the limit approaches they narrow by themselves."
    )


# Тариф Plus заметно уже остальных: держать на нём десять воркеров
# значит сжечь окно за один прогон. Решение пользователя от 14 сентября.
PLUS_DEFAULT_WORKERS = 3
STANDARD_DEFAULT_WORKERS = 10
_PLUS_PLANS = frozenset({"plus", "chatgpt-plus", "plus-monthly"})


def _burns_without_a_wall(credits: Mapping[str, Any]) -> bool:
    """Аккаунт, которому окно лимита не стена.

    Безлимит и подключённое автосписание - это одно и то же положение:
    расход продолжается за окном, упереться не во что. Прежде кредиты
    считались смягчающим обстоятельством и всё равно сужали ёмкость -
    то есть ограничивали того, кто как раз и платит за отсутствие
    ограничений.
    """

    return bool(credits.get("unlimited") or credits.get("hasCredits"))


def default_workers(limits: Mapping[str, Any] | None) -> int:
    """Сколько воркеров ставить, когда человек ничего не сказал."""

    snapshot = _snapshot(limits)
    plan_type = str(snapshot.get("planType") or "").strip().lower()
    if plan_type in _PLUS_PLANS:
        return PLUS_DEFAULT_WORKERS
    return STANDARD_DEFAULT_WORKERS


# Внутренние имена тарифов человеку не показываются: свой план он читает
# как "Pro", а событие App Server называет его "prolite". Показать слаг
# значило бы сообщить пользователю неправду о его же подписке, а
# незнакомый слаг - ещё и выдумать тариф, которого он не знает.
_PLAN_NAMES = {
    "plus": "Plus",
    "chatgpt-plus": "Plus",
    "plus-monthly": "Plus",
    "pro": "Pro",
    "prolite": "Pro",
    "chatgpt-pro": "Pro",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
}


def _is_plus(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _PLUS_PLANS


def _human_plan_name(raw: Any) -> str:
    """Имя тарифа так, как его знает человек, или пусто."""

    return _PLAN_NAMES.get(str(raw or "").strip().lower(), "")
