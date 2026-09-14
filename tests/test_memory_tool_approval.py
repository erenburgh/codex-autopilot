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
