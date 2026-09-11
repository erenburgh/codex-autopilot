"""Правило R21: приёмка выполняется в чистом окружении.

Результат, воспроизводимый только в окружении автора, не является
подтверждением. Эти тесты падают, если сьют снова начнёт зависеть
от переменных, которых нет в заявленном CI.

История: 260 тестов проходили при выставленном CODEX_THREAD_ID и давали
31 ошибку без него. Ни один тест переменную не выставлял, а CI
запускается в чистом окружении. Восемь самопринятых задач этого
не заметили, потому что каждая прогоняла тесты у себя.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src" / "codex_autopilot"

# Переменные, существующие только внутри живой Codex-сессии.
SESSION_SCOPED_ENV = ("CODEX_THREAD_ID", "CODEX_TURN_ID", "CODEX_SESSION_ID")

# tests/_relay.py - санкционированный помощник: он существует именно
# затем, чтобы identity передавалась явно, и сам ничего не читает.
SANCTIONED_HELPERS = {"_relay.py"}

# Точки, где продакшену законно читать identity из окружения.
# Список намеренно точный: рост числа точек должен быть заметен.
# lifecycle.py -> lifecycle_reservations.py: чтение переехало вместе
# с reserve_ready_frontier при разрезе lifecycle на модули.
# Число мест не изменилось, изменилось одно имя файла.
DECLARED_PRODUCTION_READS = {
    ("cli.py", 'os.environ.get("CODEX_THREAD_ID")'),
    ("lifecycle_reservations.py", 'os.environ.get("CODEX_THREAD_ID")'),
}

# Только identity живой сессии. Собственные переменные продукта
# (CODEX_AUTOPILOT_*) к делу не относятся и законны.
_ENV_READ = re.compile(
    r"""os\.environ(?:\.get\(|\[)\s*["'](""" + "|".join(SESSION_SCOPED_ENV) + r""")["']"""
)


def _test_sources() -> list[Path]:
    skip = {Path(__file__).name} | SANCTIONED_HELPERS
    return [p for p in sorted(TESTS_DIR.glob("*.py")) if p.name not in skip]


class CleanEnvironmentTests(unittest.TestCase):
    def test_no_test_module_reads_session_scoped_environment(self) -> None:
        offenders = [
            f"{path.name}: {name}"
            for path in _test_sources()
            for name in _ENV_READ.findall(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            offenders,
            [],
            "тест не должен читать переменную живой Codex-сессии; "
            "передавай identity явно, как это делает tests/_relay.py",
        )

    def test_frontier_reservation_is_imported_through_the_explicit_helper(self) -> None:
        block_import = re.compile(
            r"from\s+codex_autopilot\.lifecycle\s+import\s+\(([^)]*)\)", re.S
        )
        flat_import = re.compile(
            r"from\s+codex_autopilot\.lifecycle\s+import\s+[^\n(]*reserve_ready_frontier"
        )
        direct: set[str] = set()
        for path in _test_sources():
            text = path.read_text(encoding="utf-8")
            if any("reserve_ready_frontier" in b for b in block_import.findall(text)):
                direct.add(path.name)
            if flat_import.search(text):
                direct.add(path.name)
        self.assertEqual(
            sorted(direct),
            [],
            "reserve_ready_frontier в тестах импортируется из _relay, "
            "иначе identity владельца снова утечёт в os.environ",
        )

    def test_production_environment_reads_stay_declared(self) -> None:
        found = {
            (path.name, f'os.environ.get("{name}")')
            for path in sorted(SRC_DIR.glob("*.py"))
            for name in _ENV_READ.findall(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            found,
            DECLARED_PRODUCTION_READS,
            "изменился набор мест, где продакшен читает identity из окружения; "
            "если это осознанно, обнови DECLARED_PRODUCTION_READS и объясни рост",
        )


if __name__ == "__main__":
    unittest.main()
