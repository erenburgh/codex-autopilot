"""Написанное и никем не вызванное - это обещание без исполнения.

Один и тот же дефект за день нашёлся четырежды, и каждый раз дорого:

- ``audit_creation_causality`` проверял правило R1 и вызывался только из
  тестов: утверждение «цепочка причинности проверяется» не подкреплялось
  ничем;
- ``build_pipeline_engineer_prompt`` был снят как неиспользуемый, а на
  деле дорожку дежурного инженера просто не дописали;
- ``reconcile_desktop_runtime`` - функция восстановления после падения -
  тоже вызывалась только из тестов, поэтому мёртвая сессия не
  возвращалась в работу никогда;
- ``rate_limits``/``rate_limit_reset_at`` добывали время сброса лимита,
  которого барьер никогда не получал.

Тест закрывает класс целиком: у каждого публичного определения в
продакшене обязан быть путь вызова. Исключение допустимо, но только
именное и с причиной - список ниже читается как контракт, а не как
свалка.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"

# Точка входа, класс-контракт или сознательно публичный API. Причина
# обязательна: строка без причины - это возвращение той же болезни.
ALLOWED = {
    "main": "точка входа CLI",
    "handle_stop_hook": "вызывается Codex как хук, не нами",
    "handle_prompt_hook": "вызывается Codex как хук, не нами",
    "handle_post_tool_hook": "вызывается Codex как хук, не нами",
    "handle_interrupt_hook": "вызывается Codex как хук, не нами",
}


def _public_definitions() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    found[node.name] = path.name
    return found


def _references(name: str, blob: str) -> int:
    """Сколько раз имя встречается не как собственное определение."""

    return blob.count(name) - blob.count(f"def {name}(")


class NoUnreachableContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blob = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(SRC.glob("*.py"))
        )

    def test_every_public_function_has_a_production_caller(self) -> None:
        orphans = []
        for name, module in _public_definitions().items():
            if name in ALLOWED:
                continue
            if _references(name, self.blob) <= 0:
                orphans.append(f"{module}:{name}")
        self.assertEqual(
            orphans,
            [],
            "написано и никем не вызывается — либо подключить, либо снять, "
            "либо внести в ALLOWED с причиной: " + ", ".join(orphans),
        )

    def test_every_exemption_carries_a_reason(self) -> None:
        """Список исключений - контракт. Пустая причина его обнуляет."""

        for name, reason in ALLOWED.items():
            with self.subTest(name=name):
                self.assertTrue(reason.strip(), name)

    def test_the_exemption_list_has_no_stale_entries(self) -> None:
        """Исключение для того, чего уже нет, прячет следующую дыру."""

        defined = _public_definitions()
        for name in ALLOWED:
            with self.subTest(name=name):
                self.assertIn(name, defined)


if __name__ == "__main__":
    unittest.main()
