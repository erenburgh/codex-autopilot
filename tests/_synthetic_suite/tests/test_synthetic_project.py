"""Настоящий набор синтетического проекта для тестовых фикстур.

Лежит в каталоге `tests`, чтобы объявленной проверкой была настоящая
команда обнаружения - `unittest discover -s .../tests`, - а не запуск
файла, имя которого содержит слово suite. Имя ничего не доказывает.
"""

from __future__ import annotations

import unittest


class SyntheticProjectSuite(unittest.TestCase):
    def test_fixture_suite_runs(self) -> None:
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
