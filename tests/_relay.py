"""Тестовый помощник для резервации фронтира.

Правило R21: приёмка выполняется в чистом окружении. Сьют не имеет права
зависеть от переменных, специфичных для сессии автора.

reserve_ready_frontier требует identity владельца relay и, если её не
передали, читает CODEX_THREAD_ID из окружения. Внутри Codex-сессии
переменная есть всегда, поэтому 31 тест проходил у автора и падал
в заявленном CI. Здесь identity передаётся явно и детерминированно.
"""

from __future__ import annotations

from codex_autopilot.lifecycle import reserve_ready_frontier as _reserve_ready_frontier

TEST_RELAY_OWNER = "test-relay-owner-thread"


def reserve_ready_frontier(cfg, **kwargs):
    kwargs.setdefault("relay_owner_thread_id", TEST_RELAY_OWNER)
    return _reserve_ready_frontier(cfg, **kwargs)
