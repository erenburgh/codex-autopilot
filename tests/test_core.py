from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_autopilot.appserver import ApprovalRequired, PauseRequested, TurnResult, is_rate_limit_error, rate_limit_reset_at
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.control import arm, handle_prompt_hook, handle_stop_hook
from codex_autopilot.control import status_text
from codex_autopilot.cli import uninstall
from codex_autopilot.models import MODEL_IDS, ModelRoutingError, logical_model, resolve_reasoning, resolve_selection
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.orchestrator import DesktopOrchestrator, OrchestrationError, build_worker_prompt, match_saved_project, parse_worker_status
from codex_autopilot.plan import load_plan, validate_plan
from codex_autopilot.preflight import REQUIRED_MEMORY_TOOLS
from codex_autopilot.reasoning import next_level, normalize
from codex_autopilot.run_state import RunState, StateStore


ROOT = Path(__file__).resolve().parents[1]
ADAPTIVE_SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
HOST_SKILL = ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md"


def model_catalog() -> list[dict]:
    return [
        {"id": MODEL_IDS["sol"], "model": MODEL_IDS["sol"], "displayName": "GPT-5.6-Sol", "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("medium", "high", "xhigh", "max")]},
        {"id": MODEL_IDS["astra"], "model": MODEL_IDS["astra"], "displayName": "GPT-6-Astra", "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("medium", "high", "xhigh", "max")]},
    ]


def make_project(profile: str = "adaptive", count: int = 2, *, strategy: str | None = None, modes: list[str] | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="codex-autopilot-test-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".codex-autopilot").mkdir()
    milestones = []
    for index in range(count):
        mode = modes[index] if modes else "code"
        item = {
            "title": f"Step {index + 1}",
            "objective": f"Do step {index + 1}",
            "definition_of_done": [f"step {index + 1} verified"],
            "execution_mode": mode,
            "execution_mode_reason": "A real GUI is required by the Definition of Done." if mode == "computer_use" else "Repository files and shell verification are sufficient.",
        }
        if profile == "adaptive":
            item["reasoning"] = "medium"
        milestones.append(item)
    plan_file = root / ".codex-autopilot/bootstrap-plan.json"
    plan_file.write_text(json.dumps({"goal": "Test goal", "model_strategy": strategy or ("auto" if profile == "adaptive" else "host-settings"), "milestones": milestones}), encoding="utf-8")
    initialize_project(root, plan_file, profile=profile, skill_path=ADAPTIVE_SKILL if profile == "adaptive" else HOST_SKILL)
    return root


def update_handoff(root: Path, label: str) -> None:
    state_dir = root / ".codex-autopilot"
    (state_dir / "HANDOFF.md").write_text(f"# Handoff\n\n{label}\n", encoding="utf-8")


def completed_turn(status: str, turn_id: str = "turn") -> dict:
    reason = "\nCOMPUTER_USE_REASON: The Definition of Done requires interaction with a real browser GUI." if status == "REQUIRE_COMPUTER_USE" else ""
    return {"id": turn_id, "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": f"finished{reason}\nAUTOPILOT_STATUS: {status}"}]}


class FakeClient:
    statuses: list[str] = []
    instances: list["FakeClient"] = []
    root: Path
    catalog = model_catalog()

    def __init__(self, *_args, **_kwargs):
        self.events: list[tuple] = []
        self.efforts: list[str | None] = []
        self.thread_number = 0
        self.active = 0
        self.models_requested = 0
        self.models: list[str | None] = []
        self.__class__.instances.append(self)

    def connect(self): return {"userAgent": "fake/1"}
    def close(self): pass
    def list_permission_profiles(self, _cwd): return [{"id": ":workspace", "allowed": True}]
    def list_projects(self): return []
    def read_project(self, _project_id): raise AssertionError
    def name_thread(self, thread_id, name): self.events.append(("name", thread_id, name))
    def list_threads(self, *_args): return []
    def rate_limits(self): return {}
    def list_models(self):
        self.models_requested += 1
        return self.catalog

    def start_thread(self, *, cwd, permission_profile, project_id, model, ephemeral=False, project_memory=True):
        self.assert_no_active()
        self.thread_number += 1
        thread_id = f"thread-{self.thread_number}"
        self.models.append(model)
        self.events.append(("thread/start", thread_id, model))
        return {"thread": {"id": thread_id, "cwd": str(cwd), "projectId": project_id}, "activePermissionProfile": {"id": permission_profile}, "model": model}

    def list_mcp_server_status(self, _thread_id):
        return [{"name": "codex_autopilot_memory", "runtimeStatus": "connected", "tools": {name: {} for name in REQUIRED_MEMORY_TOOLS}}]

    def call_mcp_tool(self, _thread_id, _server, _tool, _arguments):
        return {"structuredContent": {"project_root": str(self.root), "initialized": True}}

    def assert_no_active(self):
        if self.active:
            raise AssertionError("overlapping model turns")

    def start_turn(self, *, thread_id, prompt, effort, client_user_message_id, skill_name, skill_path):
        self.assert_no_active()
        self.active = 1
        self.efforts.append(effort)
        self.events.append(("turn/start", thread_id, effort, skill_name, str(skill_path)))
        update_handoff(self.root, f"worker-{len(self.efforts)}")
        state = StateStore(self.root / ".codex-autopilot").load()
        ProjectMemory(self.root).record_evidence(
            kind="file",
            summary=f"Fake worker {len(self.efforts)} inspected the roadmap.",
            path="ROADMAP.md",
            milestone_id=state.milestone_id,
            created_by=f"worker-{len(self.efforts)}",
        )
        return {"turn": {"id": f"turn-{len(self.efforts)}"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        status = self.statuses.pop(0)
        self.events.append(("turn/completed", thread_id, turn_id, status))
        self.active = 0
        return TurnResult(thread_id, completed_turn(status, turn_id), [])

    def read_thread(self, _thread_id): return {"turns": []}


class CapabilityClient(FakeClient):
    roadmap_was_unadvanced = False

    def start_thread(self, **kwargs):
        if self.thread_number == 1:
            self.__class__.roadmap_was_unadvanced = "- [ ] M1" in (self.root / "ROADMAP.md").read_text()
        return super().start_thread(**kwargs)


class RateOnceClient(FakeClient):
    waits = 0

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        self.__class__.waits += 1
        self.active = 0
        if self.waits == 1:
            return TurnResult(thread_id, {"id": turn_id, "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}, "items": []}, [])
        return TurnResult(thread_id, completed_turn("DONE", turn_id), [])


class ApprovalClient(FakeClient):
    interrupted: list[tuple[str, str]] = []

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        raise ApprovalRequired({"method": "mcpServer/elicitation/request", "id": 1, "params": {"threadId": thread_id, "turnId": turn_id}})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.active = 0
        self.__class__.interrupted.append((thread_id, turn_id))


class NoEvidenceClient(FakeClient):
    def start_turn(self, *, thread_id, prompt, effort, client_user_message_id, skill_name, skill_path):
        self.assert_no_active()
        self.active = 1
        self.efforts.append(effort)
        update_handoff(self.root, "claimed complete without evidence")
        return {"turn": {"id": "turn-no-evidence"}}


class DisconnectedMemoryClient(FakeClient):
    def list_mcp_server_status(self, _thread_id):
        return [{"name": "codex_autopilot_memory", "runtimeStatus": "failed", "tools": {}}]


class CoreTests(unittest.TestCase):
    def tearDown(self):
        FakeClient.instances.clear()
        FakeClient.catalog = model_catalog()

    def test_reasoning_contract(self):
        self.assertEqual(normalize("ultra"), "max")
        self.assertEqual(next_level("medium"), "high")
        self.assertEqual(next_level("max"), None)
        with self.assertRaises(ValueError): normalize("low")

    def test_model_registry_routes_code_to_sol_and_computer_use_to_astra(self):
        self.assertEqual(logical_model("auto", "code"), "sol")
        self.assertEqual(logical_model("auto", "computer_use"), "astra")

    def test_complex_code_stays_on_sol(self):
        choice = resolve_selection(model_catalog(), strategy="auto", execution_mode="code", requested_reasoning="max", execution_reason="A difficult networking race is debugged from code and logs.")
        self.assertEqual(choice.model_id, MODEL_IDS["sol"])
        self.assertEqual(choice.reasoning, "max")

    def test_unsupported_reasoning_resolves_to_nearest_advertised_effort(self):
        resolved, adjustment = resolve_reasoning("high", ("medium", "xhigh"))
        self.assertEqual(resolved, "medium")
        self.assertIn("resolved", adjustment)

    def test_model_unavailable_blocks_without_fallback(self):
        root = make_project("adaptive", 1)
        FakeClient.root = root
        FakeClient.catalog = [model_catalog()[1]]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 78)
        state = StateStore(root / ".codex-autopilot").load()
        self.assertIn(MODEL_IDS["sol"], state.last_error)
        self.assertEqual(FakeClient.instances[-1].models, [])

    def test_astra_only_routes_code_to_astra(self):
        root = make_project("adaptive", 1, strategy="astra-only")
        FakeClient.root = root
        FakeClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 0)
        self.assertEqual(FakeClient.instances[-1].models, [MODEL_IDS["astra"]])

    def test_auto_computer_use_routes_to_astra(self):
        root = make_project("adaptive", 1, strategy="auto", modes=["computer_use"])
        FakeClient.root = root
        FakeClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 0)
        self.assertEqual(FakeClient.instances[-1].models, [MODEL_IDS["astra"]])

    def test_sol_only_code_routes_to_sol(self):
        root = make_project("adaptive", 1, strategy="sol-only", modes=["code"])
        FakeClient.root = root
        FakeClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 0)
        self.assertEqual(FakeClient.instances[-1].models, [MODEL_IDS["sol"]])

    def test_sol_only_computer_use_blocks(self):
        root = make_project("adaptive", 1, strategy="sol-only", modes=["computer_use"])
        FakeClient.root = root
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 78)
        state = StateStore(root / ".codex-autopilot").load()
        self.assertIn("configured as Sol-only", state.last_error)
        self.assertEqual(FakeClient.instances[-1].models, [])

    def test_require_computer_use_retries_same_milestone_in_fresh_astra(self):
        root = make_project("adaptive", 1, strategy="auto", modes=["code"])
        CapabilityClient.root = root
        CapabilityClient.statuses = ["REQUIRE_COMPUTER_USE", "DONE"]
        CapabilityClient.roadmap_was_unadvanced = False
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=CapabilityClient).run(), 0)
        state = StateStore(root / ".codex-autopilot").load()
        client = CapabilityClient.instances[-1]
        self.assertEqual(client.models, [MODEL_IDS["sol"], MODEL_IDS["astra"]])
        self.assertEqual(state.milestone_index, 0)
        self.assertTrue(CapabilityClient.roadmap_was_unadvanced)
        self.assertEqual([item["milestone_id"] for item in state.worker_history], ["M1", "M1"])
        self.assertEqual([item["status"] for item in state.worker_history], ["REQUIRE_COMPUTER_USE", "DONE"])

    def test_shared_rate_limit_retries_same_model_without_fallback(self):
        root = make_project("adaptive", 1)
        RateOnceClient.root = root
        RateOnceClient.statuses = []
        RateOnceClient.waits = 0
        clock = [0]
        def now():
            clock[0] += 100
            return clock[0]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=RateOnceClient, sleep_fn=lambda _seconds: None, now_fn=now).run(), 0)
        state = StateStore(root / ".codex-autopilot").load()
        self.assertEqual(RateOnceClient.instances[-1].models, [MODEL_IDS["sol"], MODEL_IDS["sol"]])
        self.assertEqual([item["status"] for item in state.worker_history], ["RATE_LIMITED", "DONE"])

    def test_approval_request_blocks_and_interrupts_active_worker(self):
        root = make_project("adaptive", 1)
        ApprovalClient.root = root
        ApprovalClient.interrupted = []
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=ApprovalClient).run(), 78)
        state = StateStore(root / ".codex-autopilot").load()
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.worker_history[-1]["status"], "BLOCKED")
        self.assertEqual(ApprovalClient.interrupted, [("thread-1", "turn-1")])
        self.assertIsNone(state.current_thread_id)
        self.assertIsNone(state.current_turn_id)
        self.assertEqual(state.previous_thread_ids, ["thread-1"])

    def test_plan_has_one_adaptive_source(self):
        item = {"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "high"}
        plan = validate_plan({"goal": "g", "model_strategy": "auto", "milestones": [item]}, "adaptive")
        self.assertEqual(plan.milestones[0].reasoning, "high")
        with self.assertRaises(ValueError): validate_plan({"goal": "g", "model_strategy": "auto", "milestones": [{key: value for key, value in item.items() if key != "reasoning"}]}, "adaptive")

    def test_host_plan_rejects_reasoning(self):
        item = {"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "high"}
        with self.assertRaises(ValueError): validate_plan({"goal": "g", "model_strategy": "host-settings", "milestones": [item]}, "host-settings")

    def test_status_protocol_profile_specific(self):
        self.assertEqual(parse_worker_status("x\nAUTOPILOT_STATUS: ESCALATE", True), "ESCALATE")
        require = "COMPUTER_USE_REASON: A real browser click is required.\nAUTOPILOT_STATUS: REQUIRE_COMPUTER_USE"
        self.assertEqual(parse_worker_status(require, True, True), "REQUIRE_COMPUTER_USE")
        with self.assertRaises(OrchestrationError): parse_worker_status("AUTOPILOT_STATUS: REQUIRE_COMPUTER_USE", True, False)
        with self.assertRaises(OrchestrationError): parse_worker_status("AUTOPILOT_STATUS: ESCALATE", False)
        with self.assertRaises(OrchestrationError): parse_worker_status("AUTOPILOT_STATUS: DONE\ntext", True)

    def test_bootstrap_creates_only_documented_state(self):
        root = make_project()
        names = {p.name for p in (root / ".codex-autopilot").iterdir()}
        self.assertTrue({"config.toml", "plan.json", "MILESTONE.md", "PROJECT_STATE.md", "DECISIONS.md", "HANDOFF.md", "run-state.json", "memory.sqlite3"}.issubset(names))
        self.assertTrue(names.issubset({"config.toml", "plan.json", "MILESTONE.md", "PROJECT_STATE.md", "DECISIONS.md", "HANDOFF.md", "run-state.json", "memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm"}))
        self.assertTrue((root / "ROADMAP.md").is_file())
        self.assertFalse((root / ".git/refs/heads/main").exists())

    def test_replace_removes_stale_terminal_markers(self):
        root = make_project()
        state_dir = root / ".codex-autopilot"
        (state_dir / "BLOCKED.json").write_text("{}")
        (state_dir / "pause-requested").write_text("pause")
        (state_dir / "logs").mkdir()
        (state_dir / "logs/old.log").write_text("old")
        plan_file = state_dir / "bootstrap-plan.json"
        plan_file.write_text(json.dumps({"goal": "new", "model_strategy": "auto", "milestones": [{"title": "new", "objective": "new", "definition_of_done": ["done"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "medium"}]}))
        initialize_project(root, plan_file, profile="adaptive", skill_path=ADAPTIVE_SKILL, replace=True)
        self.assertFalse((state_dir / "BLOCKED.json").exists())
        self.assertFalse((state_dir / "pause-requested").exists())
        self.assertFalse((state_dir / "logs").exists())

    def test_non_git_is_rejected_without_initializing(self):
        root = Path(tempfile.mkdtemp())
        plan = root / "plan.json"
        plan.write_text('{"goal":"g","milestones":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Git repository"):
            initialize_project(root, plan, profile="adaptive", skill_path=ADAPTIVE_SKILL)
        self.assertFalse((root / ".git").exists())

    def test_adaptive_serial_rotation(self):
        root = make_project("adaptive", 2)
        FakeClient.root = root
        FakeClient.statuses = ["ROTATE", "DONE"]
        code = DesktopOrchestrator(load_config(root), client_factory=FakeClient).run()
        state = StateStore(root / ".codex-autopilot").load()
        client = FakeClient.instances[-1]
        self.assertEqual(code, 0)
        self.assertEqual(state.status, "DONE")
        self.assertEqual(state.previous_thread_ids, ["thread-1", "thread-2"])
        self.assertEqual(client.efforts, ["medium", "medium"])
        self.assertEqual(client.models, [MODEL_IDS["sol"], MODEL_IDS["sol"]])
        self.assertEqual([e[0] for e in client.events if e[0].startswith("turn/")], ["turn/start", "turn/completed", "turn/start", "turn/completed"])
        self.assertEqual([item["model_id"] for item in state.worker_history], [MODEL_IDS["sol"], MODEL_IDS["sol"]])
        self.assertEqual([item["execution_mode"] for item in state.worker_history], ["code", "code"])
        visible_status = status_text(root)
        self.assertIn("model=GPT-5.6 Sol", visible_status)
        self.assertIn("execution_mode=code", visible_status)

    def test_adaptive_escalates_in_fresh_thread(self):
        root = make_project("adaptive", 1)
        FakeClient.root = root
        FakeClient.statuses = ["ESCALATE", "DONE"]
        code = DesktopOrchestrator(load_config(root), client_factory=FakeClient).run()
        client = FakeClient.instances[-1]
        self.assertEqual(code, 0)
        self.assertEqual(client.efforts, ["medium", "high"])
        self.assertEqual(client.models, [MODEL_IDS["sol"], MODEL_IDS["sol"]])
        self.assertEqual(StateStore(root / ".codex-autopilot").load().previous_thread_ids, ["thread-1", "thread-2"])

    def test_escalate_at_max_blocks(self):
        root = make_project("adaptive", 1)
        plan_data = json.loads((root / ".codex-autopilot/plan.json").read_text())
        plan_data["milestones"][0]["reasoning"] = "max"
        (root / ".codex-autopilot/plan.json").write_text(json.dumps(plan_data), encoding="utf-8")
        FakeClient.root = root
        FakeClient.statuses = ["ESCALATE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 78)
        self.assertIn("at max", StateStore(root / ".codex-autopilot").load().last_error)

    def test_host_omits_effort_and_prompt_escalation(self):
        root = make_project("host-settings", 1)
        cfg = load_config(root)
        state = StateStore(cfg.state_dir).load()
        prompt = build_worker_prompt(cfg, state, load_plan(cfg.state_dir, cfg.profile))
        self.assertNotIn("AUTOPILOT_STATUS: ESCALATE", prompt)
        FakeClient.root = root
        FakeClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(cfg, client_factory=FakeClient).run(), 0)
        self.assertEqual(FakeClient.instances[-1].efforts, [None])
        self.assertEqual(FakeClient.instances[-1].models, [None])
        self.assertEqual(FakeClient.instances[-1].models_requested, 0)

    def test_rate_limit_detection_and_reset(self):
        self.assertTrue(is_rate_limit_error({"nested": {"codexErrorInfo": "usageLimitExceeded"}}))
        snapshot = {"rateLimitsByLimitId": {"weekly": {"secondary": {"usedPercent": 100, "resetsAt": 999}}}}
        self.assertEqual(rate_limit_reset_at(snapshot), 999)

    def test_rate_limit_uses_only_the_exhausted_window_reset(self):
        snapshot = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 100, "resetsAt": 1788878029},
                    "secondary": {"usedPercent": 16, "resetsAt": 1789464829},
                    "rateLimitReachedType": "rate_limit_reached",
                    "spendControlReached": False,
                }
            }
        }
        self.assertEqual(rate_limit_reset_at(snapshot), 1788878029)

    def test_arm_and_stop_hook_pass_initiator_ids(self):
        root = make_project()
        arm(root)
        with mock.patch("codex_autopilot.control.spawn_dispatcher", return_value=42) as spawn, mock.patch("codex_autopilot.control.wait_for_dispatcher", return_value="WAITING_INITIATOR"):
            output = handle_stop_hook({"cwd": str(root), "session_id": "session", "turn_id": "turn"})
        spawn.assert_called_once_with(root.resolve(), initiator_thread_id="session", initiator_turn_id="turn")
        self.assertIn("dispatcher started", output["systemMessage"])
        self.assertFalse((root / ".codex-autopilot/launch-request.json").exists())

    def test_stop_hook_claims_target_outside_initiating_cwd(self):
        root = make_project()
        outside = Path(tempfile.mkdtemp(prefix="codex-autopilot-initiator-outside-"))
        launch_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-launch-registry-")) / "requests"
        with mock.patch.dict(os.environ, {"CODEX_AUTOPILOT_LAUNCH_DIR": str(launch_dir)}):
            arm(root)
            with mock.patch("codex_autopilot.control.spawn_dispatcher", return_value=43) as spawn, mock.patch("codex_autopilot.control.wait_for_dispatcher", return_value="WAITING_INITIATOR"):
                output = handle_stop_hook({"cwd": str(outside), "session_id": "outside-session", "turn_id": "outside-turn"})
        spawn.assert_called_once_with(root.resolve(), initiator_thread_id="outside-session", initiator_turn_id="outside-turn")
        self.assertIn("dispatcher started", output["systemMessage"])

    def test_completion_marker_without_memory_evidence_blocks(self):
        root = make_project("adaptive", 1)
        NoEvidenceClient.root = root
        NoEvidenceClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=NoEvidenceClient).run(), 78)
        state = StateStore(root / ".codex-autopilot").load()
        self.assertEqual(state.status, "BLOCKED")
        self.assertIn("without new Project Memory evidence", state.last_error)
        self.assertIn("- [ ] M1", (root / "ROADMAP.md").read_text())

    def test_memory_mcp_failure_blocks_before_model_turn(self):
        root = make_project("adaptive", 1)
        DisconnectedMemoryClient.root = root
        DisconnectedMemoryClient.statuses = ["DONE"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=DisconnectedMemoryClient).run(), 78)
        client = DisconnectedMemoryClient.instances[-1]
        self.assertEqual(client.efforts, [])
        self.assertEqual(StateStore(root / ".codex-autopilot").load().status, "BLOCKED")

    def test_exact_control_prompt_does_not_use_model(self):
        root = make_project()
        result = handle_prompt_hook({"cwd": str(root), "prompt": "Pause Codex Autopilot."})
        self.assertEqual(result["decision"], "block")
        self.assertTrue((root / ".codex-autopilot/pause-requested").exists())
        self.assertEqual(handle_prompt_hook({"cwd": str(root), "prompt": "Please discuss pausing Codex Autopilot"}), {})

    def test_uninstall_prompt_passes_project_and_stops_dispatcher_first(self):
        root = make_project()
        state = StateStore(root / ".codex-autopilot").load()
        state.dispatcher_pid = 12345
        StateStore(root / ".codex-autopilot").save(state)
        with mock.patch("codex_autopilot.control.subprocess.Popen") as popen:
            result = handle_prompt_hook({"cwd": str(root), "prompt": "Uninstall Codex Autopilot."})
        self.assertEqual(result["decision"], "block")
        self.assertEqual(popen.call_args.args[0][-2:], ["--project", str(root.resolve())])

        install_root = Path(tempfile.mkdtemp(prefix="codex-autopilot-uninstall-"))
        args = SimpleNamespace(yes=True, project=root, purge_project_state=False)
        with mock.patch.dict(os.environ, {"CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root)}), mock.patch("codex_autopilot.cli.pid_alive", side_effect=[True, False, False]), mock.patch("codex_autopilot.cli.shutil.which", return_value=None):
            self.assertEqual(uninstall(args), 0)
        self.assertTrue((root / ".codex-autopilot/pause-requested").exists())
        self.assertFalse(install_root.exists())

    def test_no_generic_appserver_arguments_or_automatic_commit(self):
        root = make_project()
        cfg = load_config(root)
        self.assertFalse(hasattr(cfg.desktop, "extra_args"))
        self.assertFalse(hasattr(cfg.desktop, "model"))
        self.assertFalse(cfg.auto_commit)

    def test_saved_project_longest_root_match(self):
        root = Path("/tmp/product/module")
        projects = [{"id": "broad", "roots": [{"path": "/tmp"}]}, {"id": "exact", "roots": [{"path": "/tmp/product"}]}]
        self.assertEqual(match_saved_project(root, projects)["id"], "exact")

    def test_blocked_worker_is_terminal(self):
        root = make_project("adaptive", 1)
        FakeClient.root = root
        FakeClient.statuses = ["BLOCKED"]
        self.assertEqual(DesktopOrchestrator(load_config(root), client_factory=FakeClient).run(), 78)
        self.assertEqual(StateStore(root / ".codex-autopilot").load().status, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
