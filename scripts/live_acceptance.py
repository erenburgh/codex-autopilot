#!/usr/bin/env python3
"""Developer-only live acceptance scenarios against the real Codex App Server."""
from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.appserver import AppServerClient
from codex_autopilot.config import load_config
from codex_autopilot.models import MODEL_IDS
from codex_autopilot.orchestrator import DesktopOrchestrator
from codex_autopilot.run_state import StateStore


SCENARIOS = ("sol-only", "auto-mixed", "capability-escalation")
SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
CHROME_BUNDLE_ID = "com.google.Chrome"


class ExplicitLiveApprovalClient(AppServerClient):
    """Acceptance-only client for explicitly authorized test surfaces.

    Production uses AppServerClient directly and therefore never answers an
    approval request. This class accepts only the exact Example Domain browser
    origin or Google Chrome app-selection elicitation; every other server
    request keeps the normal fail-closed behavior.
    """

    def __init__(self, *args, allowed_bundle_id: str | None, allowed_origin: str | None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.allowed_bundle_id = allowed_bundle_id
        self.allowed_origin = allowed_origin
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
        if is_allowed_app_request or is_allowed_origin_request:
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
    if name == "sol-only":
        return {
            "goal": "Live-verify three serial Sol workers.",
            "model_strategy": "sol-only",
            "milestones": [
                milestone(
                    "Create artifact A",
                    "Create artifact-a.txt containing exactly `A` followed by one newline.",
                    ["artifact-a.txt has exact required content", "checkpoint and handoff are updated"],
                    "code",
                    "This is a repository file task and needs no GUI.",
                ),
                milestone(
                    "Create artifact B",
                    "Verify artifact-a.txt, then create artifact-b.txt containing exactly `B` followed by one newline.",
                    ["artifact A is verified", "artifact-b.txt has exact required content", "checkpoint and handoff are updated"],
                    "code",
                    "Files and shell verification fully satisfy the Definition of Done.",
                ),
                milestone(
                    "Verify both artifacts",
                    "Verify both artifacts and create verified.txt containing exactly `A+B verified` followed by one newline.",
                    ["both prior artifacts are exact", "verified.txt has exact required content", "checkpoint and handoff are updated"],
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


def validate_result(project: Path, scenario: str) -> tuple[bool, dict[str, object]]:
    state = StateStore(project / ".codex-autopilot").load()
    history = state.worker_history
    models = [item.get("model_id") for item in history]
    statuses = [item.get("status") for item in history]
    milestone_ids = [item.get("milestone_id") for item in history]
    thread_ids = [str(item.get("thread_id")) for item in history]
    log = project / ".codex-autopilot/logs/app-server.jsonl"
    cua_by_worker = [contains_cua_tool_call(log, value) for value in thread_ids]
    serial_turns = no_turn_overlap(log, thread_ids)

    if scenario == "sol-only":
        expected_models = [MODEL_IDS["sol"]] * 3
        expected_statuses = ["ROTATE", "ROTATE", "DONE"]
        files_ok = all((
            exact_file(project, "artifact-a.txt", "A\n"),
            exact_file(project, "artifact-b.txt", "B\n"),
            exact_file(project, "verified.txt", "A+B verified\n"),
        ))
        scenario_ok = models == expected_models and statuses == expected_statuses and files_ok and not any(cua_by_worker) and serial_turns
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
    else:
        expected_models = [MODEL_IDS["sol"], MODEL_IDS["astra"]]
        expected_statuses = ["REQUIRE_COMPUTER_USE", "DONE"]
        files_ok = exact_file(project, "capability-verification.txt", "verified after Sol to Astra escalation\n")
        scenario_ok = models == expected_models and statuses == expected_statuses and milestone_ids == ["M1", "M1"] and files_ok and cua_by_worker == [False, True] and serial_turns

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
        "no_turn_overlap": serial_turns,
        "fresh_threads": len(thread_ids) == len(set(thread_ids)),
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
    initialize_project(project, plan_path, profile="adaptive", skill_path=SKILL, replace=args.replace)
    client_factory = AppServerClient
    if args.approve_live_test_surface:
        client_factory = partial(
            ExplicitLiveApprovalClient,
            allowed_bundle_id=CHROME_BUNDLE_ID,
            allowed_origin="https://example.com",
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
