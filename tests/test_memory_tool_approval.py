"""Инструмент памяти не спрашивает разрешение на каждом ходе.

Манифест объявлял `approval_mode: "auto"`. Такого значения в
перечислении Codex нет - официальный плагин codex-app-tools пишет
`approve` там, где спрашивать не нужно, и `prompt` там, где нужно.
Непонятое значение откатывалось к запросу, и разрешение требовалось
заново: после каждого обновления, а внутри прогона - на каждом свежем
воркере, потому что воркер по построению свежий.

Цена измерена на живом прогоне: ход реплэннера упёрся в запрос,
диспетчер на approvals не отвечает, ход остался прерванным навсегда,
дежурный инженер не смог стартовать из-за мёртвого предшественника, и
прогон из 24 задач встал с нулём выполненных.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest


PLUGINS = Path(__file__).resolve().parent.parent / "plugins"
# Значения, которые Codex понимает для инструмента MCP-сервера плагина.
ACCEPTED = {"approve", "prompt", "writes"}


class MemoryToolApprovalTests(unittest.TestCase):
    def manifests(self):
        found = sorted(PLUGINS.glob("*/.mcp.json"))
        self.assertTrue(found, "манифесты MCP не найдены")
        for path in found:
            yield path, json.loads(path.read_text(encoding="utf-8"))

    def test_every_declared_mode_is_one_codex_understands(self) -> None:
        for path, data in self.manifests():
            for name, server in data["mcpServers"].items():
                default = server.get("default_tools_approval_mode")
                if default is not None:
                    self.assertIn(default, ACCEPTED, f"{path.parent.name}/{name}")
                for tool, config in (server.get("tools") or {}).items():
                    mode = config.get("approval_mode")
                    if mode is not None:
                        self.assertIn(
                            mode, ACCEPTED, f"{path.parent.name}/{name}/{tool}"
                        )

    def test_the_memory_tool_is_approved_without_asking(self) -> None:
        """Иначе прогон упирается в человека на каждом свежем воркере."""

        for path, data in self.manifests():
            server = data["mcpServers"]["codex_autopilot_memory"]
            self.assertEqual(
                server["tools"]["memory"]["approval_mode"],
                "approve",
                path.parent.name,
            )
            self.assertEqual(
                server["default_tools_approval_mode"], "approve", path.parent.name
            )


if __name__ == "__main__":
    unittest.main()


class PluginCacheConsistencyTests(unittest.TestCase):
    """Codex обязан грузить ту же копию плагина, что установлена.

    Он читает плагин из своего кэша, а рантайм - из каталога установки.
    Пока в кэше оставалась прежняя копия, грузилась она: у пользователя
    стоял 0.9.7, а работал 0.9.0 - с объявлением Interrupt на 30 секунд.
    Codex зажимает его до 3, переписывает файл, хэш меняется, и доверие
    Stop-хука слетает на каждой загрузке. Со стороны это неотличимо от
    "хуки слетают сами", и повторным доверием не лечится.
    """

    def setUp(self) -> None:
        import tempfile
        from codex_autopilot import preflight

        self.preflight = preflight
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)

    def _plugin(self, root: Path, name: str, version: str) -> Path:
        target = root / ".codex-plugin"
        target.mkdir(parents=True, exist_ok=True)
        (target / "plugin.json").write_text(
            json.dumps({"name": name, "version": version}), encoding="utf-8"
        )
        return root

    def _cache(self, name: str, *versions: str) -> None:
        for version in versions:
            self._plugin(
                self.home / "plugins/cache/codex-autopilot-local" / name / version,
                name,
                version,
            )

    def state(self, installed_version: str):
        import os
        from unittest import mock

        plugin_root = self._plugin(
            self.home / "install" / "plugin", "codex-autopilot-adaptive", installed_version
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            return self.preflight.plugin_cache_state(plugin_root)

    # Версии здесь синтетические и намеренно не совпадают с настоящей:
    # проверяется правило, а не номер выпуска.
    INSTALLED = "7.7.7-beta.local.20260101.000000"

    def test_one_matching_copy_passes(self) -> None:
        self._cache("codex-autopilot-adaptive", self.INSTALLED)
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "OK", detail)

    def test_an_older_copy_in_the_cache_fails(self) -> None:
        self._cache("codex-autopilot-adaptive", "1.0.0-beta")
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("1.0.0-beta", detail)

    def test_two_copies_fail_even_when_the_first_one_matches(self) -> None:
        """Лишняя копия опасна сама по себе: выбирает не наш код, а Codex."""

        self._cache("codex-autopilot-adaptive", self.INSTALLED, "9.9.9-beta")
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "FAIL", detail)

    def test_a_source_tree_is_not_compared_with_a_machine_cache(self) -> None:
        """Без метки установщика перед нами исходники, а не установка."""

        self._cache("codex-autopilot-adaptive", "1.0.0-beta")
        status, _ = self.state("7.7.7-beta")
        self.assertEqual(status, "WARN")
