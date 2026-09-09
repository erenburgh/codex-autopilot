#!/usr/bin/env python3
"""Developer-only live acceptance scenarios against the real Codex App Server."""
from __future__ import annotations

import argparse
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Match the installed launcher's absolute memory-server transport. A relative
# PYTHONPATH stops resolving after App Server changes the MCP process cwd to the
# acceptance target.
os.environ.setdefault("CODEX_AUTOPILOT_RUNTIME", str(ROOT / "scripts" / "codex-autopilot-dev"))

from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.appserver import AppServerClient
from codex_autopilot.config import load_config
from codex_autopilot.models import MODEL_IDS
from codex_autopilot.orchestrator import DesktopOrchestrator
from codex_autopilot.preflight import MEMORY_SERVER_NAME, REQUIRED_MEMORY_TOOLS
from codex_autopilot.run_state import StateStore


SCENARIOS = ("sol-only", "auto-mixed", "capability-escalation", "host-settings")
ADAPTIVE_SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
HOST_SKILL = ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md"
CHROME_BUNDLE_ID = "com.google.Chrome"


class ExplicitLiveApprovalClient(AppServerClient):
    """Acceptance-only client for explicitly authorized test surfaces.

    Production uses AppServerClient directly and therefore never answers an
    approval request. This class accepts only the exact Example Domain browser
    origin or Google Chrome app-selection elicitation; every other server
    request keeps the normal fail-closed behavior.
    """

    def __init__(self, *args, allowed_bundle_id: str | None, allowed_origin: str | None, allow_memory_tools: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.allowed_bundle_id = allowed_bundle_id
        self.allowed_origin = allowed_origin
        self.allow_memory_tools = allow_memory_tools
        self.accepted_live_approvals = 0

    def _inspect_event(self, message: dict[str, object]) -> None:
        params = message.get("params") or {}
        meta = params.get("_meta") or {} if isinstance(params, dict) else {}
        tool_params = meta.get("tool_params") or {} if isinstance(meta, dict) else {}
        is_allowed_app_request = self.allowed_bundle_id is not None and all((
            message.get("method") == "mcpServer/elicitation/request",
            "id" in message,
            isinstance(params, dict) and params.get("serverName") == "cua_repl",
            isinstance(meta, dict) and meta.get("codex_approval_kind") == "mcp_tool_call",
            isinstance(meta, dict) and meta.get("connector_id") == "computer-use",
            isinstance(meta, dict) and meta.get("tool_name") == "get_app_state",
            isinstance(tool_params, dict) and tool_params.get("app") == self.allowed_bundle_id,
        ))
        is_allowed_origin_request = self.allowed_origin is not None and all((
            message.get("method") == "mcpServer/elicitation/request",
            "id" in message,
            isinstance(params, dict) and params.get("serverName") == "cua_repl",
            isinstance(meta, dict) and meta.get("codex_approval_kind") == "mcp_tool_call",
            isinstance(meta, dict) and meta.get("connector_id") == "browser-use",
            isinstance(meta, dict) and meta.get("tool_name") == "access_browser_origin",
            isinstance(tool_params, dict) and tool_params.get("origin") == self.allowed_origin,
        ))
        memory_tool = meta.get("tool_name") if isinstance(meta, dict) else None
        if memory_tool is None and isinstance(params, dict):
            match = re.fullmatch(r'Allow the codex_autopilot_memory MCP server to run tool "([a-z_]+)"\?', str(params.get("message") or ""))
            memory_tool = match.group(1) if match else None
        is_allowed_memory_request = self.allow_memory_tools and all((
            message.get("method") == "mcpServer/elicitation/request",
            "id" in message,
            isinstance(params, dict) and params.get("serverName") == MEMORY_SERVER_NAME,
            memory_tool in REQUIRED_MEMORY_TOOLS,
        ))
        if is_allowed_app_request or is_allowed_origin_request or is_allowed_memory_request:
            self.send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "action": "accept",
                    "content": {"persist": "session"},
                    "_meta": {"persist": "session"},
                },
            })
            self.accepted_live_approvals += 1
            if is_allowed_memory_request:
                print(f"[acceptance only] approved {MEMORY_SERVER_NAME}.{memory_tool} for this App Server session", flush=True)
            return
        super()._inspect_event(message)


class LiveDesktopOrchestrator(DesktopOrchestrator):
    """Developer harness that leaves time to foreground a visible GUI worker."""

    def __init__(self, *args, computer_use_start_delay: float = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.computer_use_start_delay = computer_use_start_delay

    def _start_existing_thread(self, state, prompt=None, before=None):
        if state.execution_mode == "computer_use" and self.computer_use_start_delay > 0:
            self.emit(
                f"acceptance foreground window={self.computer_use_start_delay:g}s "
                f"for thread={state.current_thread_id}"
            )
            time.sleep(self.computer_use_start_delay)
        return super()._start_existing_thread(state, prompt, before)


def milestone(title: str, objective: str, done: list[str], mode: str, reason: str) -> dict[str, object]:
    return {
        "title": title,
        "objective": objective,
        "definition_of_done": done,
        "execution_mode": mode,
        "execution_mode_reason": reason,
        "reasoning": "medium",
    }


def scenario_plan(name: str) -> dict[str, object]:
    if name == "host-settings":
        return {
            "goal": "Live-verify Host Settings field omission with one real worker.",
            "model_strategy": "host-settings",
            "milestones": [
                {
                    "title": "Create host-default artifact",
                    "objective": "Create host-settings.txt containing exactly `host defaults verified` followed by one newline, verify it directly, and record milestone evidence in Project Memory.",
                    "definition_of_done": [
                        "host-settings.txt has exact required content",
                        "M1 evidence is stored in Project Memory",
                        "checkpoint and handoff are updated",
                    ],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository files and shell verification fully satisfy the Definition of Done.",
                }
            ],
        }
    if name == "sol-only":
        return {
            "goal": "Live-verify three serial Sol workers.",
            "model_strategy": "sol-only",
            "milestones": [
                milestone(
                    "Create artifact A",
                    "Create artifact-a.txt containing exactly `A` followed by one newline. Record file evidence for M1 and create a verified Truth that artifact A has exact content.",
                    ["artifact-a.txt has exact required content", "M1 evidence and a verified FACT are stored in Project Memory", "checkpoint and handoff are updated"],
                    "code",
                    "This is a repository file task and needs no GUI.",
                ),
                milestone(
                    "Create artifact B",
                    "Use Project Memory search/get to retrieve M1's verified artifact fact and its evidence without relying on HANDOFF. Verify artifact-a.txt directly, then create artifact-b.txt containing exactly `B` followed by one newline and record M2 evidence.",
                    ["M1's verified FACT and evidence are retrieved through memory MCP", "artifact A is verified directly", "artifact-b.txt has exact required content", "M2 evidence and handoff are updated"],
                    "code",
                    "Files and shell verification fully satisfy the Definition of Done.",
                ),
                milestone(
                    "Verify both artifacts",
                    "Use Project Memory to retrieve the verified M1 FACT again, verify both artifacts directly, and create verified.txt containing exactly `A+B verified` followed by one newline.",
                    ["M1's verified FACT is retrieved in fresh Worker 3", "both prior artifacts are exact", "verified.txt has exact required content", "M3 evidence and handoff are updated"],
                    "code",
                    "The result can be verified entirely from repository files.",
                ),
            ],
        }
    if name == "auto-mixed":
        return {
            "goal": "Live-verify Sol to Astra to Sol routing with actual browser Computer Use.",
            "model_strategy": "auto",
            "milestones": [
                milestone(
                    "Create browser fixture",
                    "Create test-page.html with a prominent visible heading whose exact text is `CODEX AUTOPILOT GUI VERIFIED`, plus valid minimal HTML. Do not open a browser.",
                    ["test-page.html exists", "the exact visible heading is present", "checkpoint and handoff are updated"],
                    "code",
                    "Creating and checking an HTML file needs no GUI.",
                ),
                milestone(
                    "Verify fixture in a real browser",
                    "Use the Computer Use in-app browser entry point `cua.createBrowserTab(\"iab\", \"https://example.com\", {visible:true})` to open the public Example Domain page and visually confirm the exact heading `Example Domain`. Write gui-verification.txt with exactly `verified via Computer Use` followed by one newline. Do not use Chrome, Edge, Safari, another external application, or a shell HTTP client. Source inspection alone does not satisfy this milestone.",
                    ["a real in-app browser was controlled through Computer Use", "the exact Example Domain heading was visually observed", "gui-verification.txt has exact required content", "checkpoint and handoff are updated"],
                    "computer_use",
                    "The Definition of Done explicitly requires opening and visually checking the page in a real browser through Computer Use.",
                ),
                milestone(
                    "Verify mixed route artifacts",
                    "Verify test-page.html and gui-verification.txt from files, then create final-verification.txt containing exactly `Sol-Astra-Sol verified` followed by one newline. Do not open a browser.",
                    ["the prior artifacts have exact required content", "final-verification.txt has exact required content", "checkpoint and handoff are updated"],
                    "code",
                    "Repository files and shell checks fully satisfy this verification.",
                ),
            ],
        }
    return {
        "goal": "Live-verify AUTO capability escalation on one unchanged milestone.",
        "model_strategy": "auto",
        "milestones": [
            milestone(
                "Escalate and perform browser verification",
                "The Definition of Done requires opening an interactive browser through Computer Use and visually confirming the exact heading `Example Domain` at https://example.com. If the effective execution mode is code, do not use a GUI and do not mark the milestone complete: update the checkpoint and handoff, state the concrete browser requirement, then return REQUIRE_COMPUTER_USE. If the effective execution mode is computer_use, use the Computer Use in-app browser entry point `cua.createBrowserTab(\"iab\", \"https://example.com\", {visible:true})` to open and visually confirm the heading. Do not use an external browser application or a shell HTTP client. Write capability-verification.txt containing exactly `verified after Sol to Astra escalation` followed by one newline.",
                ["the same milestone is retained across capability escalation", "a real in-app browser was controlled through Computer Use", "the exact Example Domain heading was visually observed", "capability-verification.txt has exact required content", "checkpoint and handoff are updated"],
                "code",
                "The initial classification intentionally exercises runtime discovery: the worker must recognize that real browser interaction is required by the Definition of Done.",
            ),
        ],
    }


def contains_cua_tool_call(log_path: Path, thread_id: str) -> bool:
    """Find an actual CUA MCP item for one worker, excluding prompts and startup."""
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("method") not in {"item/started", "item/completed"}:
            continue
        params = payload.get("params") or {}
        item = params.get("item") or {}
        if params.get("threadId") == thread_id and item.get("type") == "mcpToolCall" and item.get("server") == "cua_repl":
            return True
    return False


def contains_memory_tool_call(log_path: Path, thread_id: str) -> bool:
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("method") not in {"item/started", "item/completed"}:
            continue
        params = payload.get("params") or {}
        item = params.get("item") or {}
        if params.get("threadId") == thread_id and item.get("type") == "mcpToolCall" and item.get("server") == MEMORY_SERVER_NAME:
            return True
    return False


def no_turn_overlap(log_path: Path, thread_ids: list[str]) -> bool:
    """Verify serial turn/started -> turn/completed ordering for scenario workers."""
    selected = set(thread_ids)
    active: set[str] = set()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = (json.loads(line).get("payload") or {})
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        method = payload.get("method")
        params = payload.get("params") or {}
        thread_id = params.get("threadId")
        if thread_id not in selected or method not in {"turn/started", "turn/completed"}:
            continue
        if method == "turn/started":
            if active:
                return False
            active.add(str(thread_id))
        else:
            active.discard(str(thread_id))
    return not active


def exact_file(project: Path, name: str, content: str) -> bool:
    path = project / name
    return path.is_file() and path.read_text(encoding="utf-8") == content


def rpc_exchanges(log_path: Path) -> list[dict[str, object]]:
    pending: dict[object, dict[str, object]] = {}
    exchanges: list[dict[str, object]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            envelope = json.loads(line)
            payload = envelope.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        request_id = payload.get("id")
        if envelope.get("direction") == "sent" and payload.get("method") and request_id is not None:
            pending[request_id] = payload
        elif envelope.get("direction") == "received" and request_id in pending and "method" not in payload:
            exchanges.append({"request": pending.pop(request_id), "response": payload})
    return exchanges


def validate_result(project: Path, scenario: str) -> tuple[bool, dict[str, object]]:
    state = StateStore(project / ".codex-autopilot").load()
    history = state.worker_history
    models = [item.get("model_id") for item in history]
    statuses = [item.get("status") for item in history]
    milestone_ids = [item.get("milestone_id") for item in history]
    thread_ids = [str(item.get("thread_id")) for item in history]
    log = project / ".codex-autopilot/logs/app-server.jsonl"
    cua_by_worker = [contains_cua_tool_call(log, value) for value in thread_ids]
    memory_by_worker = [contains_memory_tool_call(log, value) for value in thread_ids]
    serial_turns = no_turn_overlap(log, thread_ids)
    thread_start_requests = [
        item for item in rpc_exchanges(log)
        if (item["request"] or {}).get("method") == "thread/start"
    ]
    turn_start_requests = [
        item for item in rpc_exchanges(log)
        if (item["request"] or {}).get("method") == "turn/start"
    ]
    model_fields_omitted = all("model" not in ((item["request"] or {}).get("params") or {}) for item in thread_start_requests)
    effort_fields_omitted = all("effort" not in ((item["request"] or {}).get("params") or {}) for item in turn_start_requests)
    applied_models = [((item["response"] or {}).get("result") or {}).get("model") for item in thread_start_requests]
    applied_reasoning = [((item["response"] or {}).get("result") or {}).get("reasoningEffort") for item in thread_start_requests]

    if scenario == "sol-only":
        expected_models = [MODEL_IDS["sol"]] * 3
        expected_statuses = ["ROTATE", "ROTATE", "DONE"]
        files_ok = all((
            exact_file(project, "artifact-a.txt", "A\n"),
            exact_file(project, "artifact-b.txt", "B\n"),
            exact_file(project, "verified.txt", "A+B verified\n"),
        ))
        scenario_ok = models == expected_models and statuses == expected_statuses and files_ok and all(memory_by_worker) and not any(cua_by_worker) and serial_turns
    elif scenario == "auto-mixed":
        expected_models = [MODEL_IDS["sol"], MODEL_IDS["astra"], MODEL_IDS["sol"]]
        expected_statuses = ["ROTATE", "ROTATE", "DONE"]
        page = project / "test-page.html"
        files_ok = all((
            page.is_file() and "CODEX AUTOPILOT GUI VERIFIED" in page.read_text(encoding="utf-8"),
            exact_file(project, "gui-verification.txt", "verified via Computer Use\n"),
            exact_file(project, "final-verification.txt", "Sol-Astra-Sol verified\n"),
        ))
        scenario_ok = models == expected_models and statuses == expected_statuses and files_ok and cua_by_worker == [False, True, False] and serial_turns
    elif scenario == "capability-escalation":
        expected_models = [MODEL_IDS["sol"], MODEL_IDS["astra"]]
        expected_statuses = ["REQUIRE_COMPUTER_USE", "DONE"]
        files_ok = exact_file(project, "capability-verification.txt", "verified after Sol to Astra escalation\n")
        scenario_ok = models == expected_models and statuses == expected_statuses and milestone_ids == ["M1", "M1"] and files_ok and cua_by_worker == [False, True] and serial_turns
    else:
        expected_statuses = ["DONE"]
        files_ok = exact_file(project, "host-settings.txt", "host defaults verified\n")
        scenario_ok = all((
            models == [None],
            [item.get("reasoning") for item in history] == [None],
            statuses == expected_statuses,
            files_ok,
            memory_by_worker == [True],
            cua_by_worker == [False],
            serial_turns,
            model_fields_omitted,
            effort_fields_omitted,
            len(applied_models) == 1 and bool(applied_models[0]),
        ))

    report = {
        "scenario": scenario,
        "result": "PASS" if state.status == "DONE" and scenario_ok else "FAIL",
        "terminal_status": state.status,
        "models": models,
        "reasoning": [item.get("reasoning") for item in history],
        "execution_modes": [item.get("execution_mode") for item in history],
        "statuses": statuses,
        "milestone_ids": milestone_ids,
        "thread_ids": thread_ids,
        "computer_use_tool_call_by_worker": cua_by_worker,
        "memory_tool_call_by_worker": memory_by_worker,
        "no_turn_overlap": serial_turns,
        "fresh_threads": len(thread_ids) == len(set(thread_ids)),
        "thread_start_model_field_omitted": model_fields_omitted,
        "turn_start_effort_field_omitted": effort_fields_omitted,
        "app_server_applied_models": applied_models,
        "app_server_applied_reasoning": applied_reasoning,
        "project": str(project),
    }
    return report["result"] == "PASS", report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("project", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--approve-live-test-surface",
        action="store_true",
        help="explicitly approve only Google Chrome and https://example.com for this developer acceptance session",
    )
    parser.add_argument(
        "--approve-live-memory-tools",
        action="store_true",
        help="explicitly approve only the bundled allowlisted Project Memory tools for this developer acceptance session",
    )
    parser.add_argument(
        "--computer-use-start-delay",
        type=float,
        default=0,
        help="developer-only seconds to foreground the already-created visible Computer Use task before turn/start",
    )
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    if project.exists() and args.replace:
        shutil.rmtree(project)
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    plan_path = project / "live-plan.json"
    plan_path.write_text(json.dumps(scenario_plan(args.scenario), indent=2) + "\n", encoding="utf-8")
    profile = "host-settings" if args.scenario == "host-settings" else "adaptive"
    skill = HOST_SKILL if profile == "host-settings" else ADAPTIVE_SKILL
    initialize_project(project, plan_path, profile=profile, skill_path=skill, replace=args.replace)
    client_factory = AppServerClient
    if args.approve_live_test_surface or args.approve_live_memory_tools:
        client_factory = partial(
            ExplicitLiveApprovalClient,
            allowed_bundle_id=CHROME_BUNDLE_ID,
            allowed_origin="https://example.com",
            allow_memory_tools=args.approve_live_memory_tools,
        )
    code = LiveDesktopOrchestrator(
        load_config(project),
        client_factory=client_factory,
        computer_use_start_delay=max(0, args.computer_use_start_delay),
    ).run()
    ok, report = validate_result(project, args.scenario)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if code == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
