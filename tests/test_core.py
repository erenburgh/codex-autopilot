from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_autopilot.appserver import AppServerRpcError, ApprovalRequired, PauseRequested, TurnResult, is_rate_limit_error, rate_limit_reset_at
from _gates import patch_hook_trust_gates
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from codex_autopilot.control import arm, handle_prompt_hook, handle_stop_hook
from codex_autopilot.control import status_text
from codex_autopilot.cli import uninstall
from codex_autopilot.models import MODEL_IDS, ModelRoutingError, logical_model, resolve_reasoning, resolve_selection
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.project_association import match_saved_project
from codex_autopilot.plan import load_plan, validate_plan
from codex_autopilot.preflight import REQUIRED_MEMORY_TOOLS
from codex_autopilot.reasoning import normalize
from codex_autopilot.resources import LockOwner, acquire_resources_in_state
from codex_autopilot.run_state import RunState, StateStore, utc_now
from codex_autopilot.task_state import TaskState, transition_task


ROOT = Path(__file__).resolve().parents[1]
ADAPTIVE_SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
HOST_SKILL = ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md"


def model_catalog() -> list[dict]:
    return [
        {"id": MODEL_IDS["sol"], "model": MODEL_IDS["sol"], "displayName": "GPT-5.6-Sol", "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("medium", "high", "xhigh", "max")]},
        {"id": MODEL_IDS["astra"], "model": MODEL_IDS["astra"], "displayName": "GPT-6-Astra", "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("medium", "high", "xhigh", "max")]},
    ]


def make_project(
    profile: str = "adaptive",
    count: int = 2,
    *,
    strategy: str | None = None,
    modes: list[str] | None = None,
    language: str = "en",
    desktop_project_id: str = "desktop-project",
) -> Path:
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
    initialize_project(
        root,
        plan_file,
        profile=profile,
        skill_path=ADAPTIVE_SKILL if profile == "adaptive" else HOST_SKILL,
        language=language,
        # Поверхность одна - desktop_owned, и она требует проект. Прежде
        # умолчанием был headless, и фикстура обходилась без него.
        desktop_project_id=desktop_project_id,
    )
    return root


def update_handoff(root: Path, label: str) -> None:
    state_dir = root / ".codex-autopilot"
    (state_dir / "HANDOFF.md").write_text(f"# Handoff\n\n{label}\n", encoding="utf-8")


def completed_turn(status: str, turn_id: str = "turn") -> dict:
    reason = "\nCOMPUTER_USE_REASON: The Definition of Done requires interaction with a real browser GUI." if status == "REQUIRE_COMPUTER_USE" else ""
    return {"id": turn_id, "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": f"finished{reason}\nAUTOPILOT_STATUS: {status}"}]}


@contextlib.contextmanager
def isolated_launch_registry():
    """Держать реестр взведённых стартов в стороне от общего.

    Реестр живёт по фиксированному пути в TMPDIR, один на пользователя.
    Тест, взводивший старт мимо этой изоляции, оставлял в нём запись про
    свой временный каталог, и живой Stop-хук потом отказывался запускать
    что-либо: "multiple Autopilot starts are armed".
    """

    directory = Path(tempfile.mkdtemp(prefix="codex-autopilot-launch-registry-")) / "requests"
    with mock.patch.dict(os.environ, {"CODEX_AUTOPILOT_LAUNCH_DIR": str(directory)}):
        yield directory


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
        self.project_ids: list[str | None] = []
        self.cwds: list[Path] = []
        self.plugin_id: str | None = None
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

    def start_thread(self, *, cwd, permission_profile, project_id, model, plugin_root=None, ephemeral=False, project_memory=True):
        self.assert_no_active()
        self.thread_number += 1
        thread_id = f"thread-{self.thread_number}"
        self.models.append(model)
        self.project_ids.append(project_id)
        self.cwds.append(Path(cwd))
        self.plugin_id = f"{Path(plugin_root).name}@codex-autopilot-local" if plugin_root else None
        self.events.append(("thread/start", thread_id, model))
        return {"thread": {"id": thread_id, "cwd": str(cwd), "projectId": project_id}, "activePermissionProfile": {"id": permission_profile}, "model": model}

    def list_mcp_server_status(self, _thread_id):
        return [{"name": "codex_autopilot_memory", "runtimeStatus": "connected", "pluginId": self.plugin_id, "tools": {name: {} for name in REQUIRED_MEMORY_TOOLS}}]

    def call_mcp_tool(self, _thread_id, _server, _tool, _arguments):
        return {"structuredContent": {"project_root": str(self.root), "initialized": True}}

    def assert_no_active(self):
        if self.active:
            raise AssertionError("overlapping model turns")

    def start_turn(self, *, thread_id, prompt, effort, client_user_message_id, skill_name, skill_path, cwd):
        self.assert_no_active()
        if Path(cwd).resolve() != self.root.resolve():
            raise AssertionError("turn/start did not use the canonical target cwd")
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
    def start_turn(self, *, thread_id, prompt, effort, client_user_message_id, skill_name, skill_path, cwd):
        self.assert_no_active()
        if Path(cwd).resolve() != self.root.resolve():
            raise AssertionError("turn/start did not use the canonical target cwd")
        self.active = 1
        self.efforts.append(effort)
        update_handoff(self.root, "claimed complete without evidence")
        return {"turn": {"id": "turn-no-evidence"}}


class DisconnectedMemoryClient(FakeClient):
    def list_mcp_server_status(self, _thread_id):
        return [{"name": "codex_autopilot_memory", "runtimeStatus": "failed", "tools": {}}]


class ExplicitProjectClient(FakeClient):
    def read_project(self, project_id):
        return {"id": project_id, "name": "Initiating Project", "roots": [{"path": "/different/filesystem/root"}]}


class ProjectSlotClient(FakeClient):
    def __init__(self, *_args, **_kwargs):
        super().__init__(*_args, **_kwargs)
        self.slot_id = "slot-1"
        self.slot_cwd = Path("/desktop/project")
        self.slot_model = MODEL_IDS["sol"]
        self.plugin_id = "codex-autopilot-adaptive@codex-autopilot-local"
        self.handoff_turn = False
        self.production_kwargs: dict | None = None

    def resume_thread(self, thread_id):
        self.slot_id = thread_id
        self.events.append(("thread/resume", thread_id))
        return {
            "thread": {"id": thread_id, "cwd": str(self.slot_cwd), "projectId": None, "model": self.slot_model},
            "activePermissionProfile": {"id": ":workspace"},
            "model": self.slot_model,
        }

    def start_plain_turn(self, *, thread_id, prompt, effort, client_user_message_id, cwd, permission_profile, model):
        self.assert_no_active()
        self.active = 1
        self.handoff_turn = True
        self.slot_cwd = Path(cwd)
        self.slot_model = model or self.slot_model
        self.events.append(("slot/handoff", thread_id, Path(cwd), permission_profile, model))
        return {"turn": {"id": "slot-handoff-turn"}}

    def start_turn(self, **kwargs):
        self.production_kwargs = dict(kwargs)
        return super().start_turn(**{
            key: kwargs[key]
            for key in (
                "thread_id",
                "prompt",
                "effort",
                "client_user_message_id",
                "skill_name",
                "skill_path",
                "cwd",
            )
        })

    def wait_for_turn(self, thread_id, turn_id, **kwargs):
        if self.handoff_turn:
            self.handoff_turn = False
            self.active = 0
            return TurnResult(
                thread_id,
                {"id": turn_id, "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": WORKSPACE_HANDOFF_OK}]},
                [],
            )
        return super().wait_for_turn(thread_id, turn_id, **kwargs)

    def read_thread(self, thread_id):
        return {"id": thread_id, "cwd": str(self.slot_cwd), "projectId": None, "model": self.slot_model, "turns": []}

    def unsubscribe_thread(self, thread_id):
        self.events.append(("thread/unsubscribe", thread_id))


class BusyProjectSlotClient(ProjectSlotClient):
    def resume_thread(self, thread_id):
        raise AppServerRpcError("thread/resume", {"message": f"thread {thread_id} already has an active writer"})


class BusyOnceProjectSlotClient(ProjectSlotClient):
    resume_attempts = 0

    def resume_thread(self, thread_id):
        self.__class__.resume_attempts += 1
        if self.resume_attempts == 1:
            raise AppServerRpcError(
                "thread/resume",
                {"message": f"thread {thread_id} already has an active writer"},
            )
        return super().resume_thread(thread_id)


class CoreTests(unittest.TestCase):
    def tearDown(self):
        FakeClient.instances.clear()
        FakeClient.catalog = model_catalog()

    def test_reasoning_contract(self):
        self.assertEqual(normalize("ultra"), "max")
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


    def test_plan_has_one_adaptive_source(self):
        item = {"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "high"}
        plan = validate_plan({"goal": "g", "model_strategy": "auto", "milestones": [item]}, "adaptive")
        self.assertEqual(plan.milestones[0].reasoning, "high")
        with self.assertRaises(ValueError): validate_plan({"goal": "g", "model_strategy": "auto", "milestones": [{key: value for key, value in item.items() if key != "reasoning"}]}, "adaptive")

    def test_host_plan_rejects_reasoning(self):
        item = {"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "high"}
        with self.assertRaises(ValueError): validate_plan({"goal": "g", "model_strategy": "host-settings", "milestones": [item]}, "host-settings")


    def test_bootstrap_creates_only_documented_state(self):
        root = make_project()
        names = {p.name for p in (root / ".codex-autopilot").iterdir()}
        self.assertTrue({"config.toml", "plan.json", "MILESTONE.md", "PROJECT_STATE.md", "DECISIONS.md", "HANDOFF.md", "handoff", "run-state.json", "memory.sqlite3"}.issubset(names))
        self.assertTrue(names.issubset({"config.toml", "plan.json", "MILESTONE.md", "PROJECT_STATE.md", "DECISIONS.md", "HANDOFF.md", "handoff", "run-state.json", "memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm", "memory.lock"}))
        self.assertTrue((root / "ROADMAP.md").is_file())
        self.assertFalse((root / ".git/refs/heads/main").exists())


    def test_invalid_run_language_is_rejected_before_state_creation(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-language-"))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        plan_file = state_dir / "bootstrap-plan.json"
        plan_file.write_text(json.dumps({"goal": "g", "model_strategy": "auto", "milestones": [{"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "medium"}]}))
        with self.assertRaisesRegex(ValueError, "BCP-47"):
            initialize_project(
                root,
                plan_file,
                profile="adaptive",
                skill_path=ADAPTIVE_SKILL,
                language="ru; rm -rf",
            )
        self.assertFalse((state_dir / "config.toml").exists())

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

    def test_bootstrap_persists_preflight_project_association(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-project-id-"))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        plan_file = state_dir / "bootstrap-plan.json"
        plan_file.write_text(json.dumps({"goal": "g", "model_strategy": "auto", "milestones": [{"title": "t", "objective": "o", "definition_of_done": ["d"], "execution_mode": "code", "execution_mode_reason": "files suffice", "reasoning": "medium"}]}))
        initialize_project(root, plan_file, profile="adaptive", skill_path=ADAPTIVE_SKILL, project_id="project-1")
        self.assertEqual(load_config(root).desktop.project_id, "project-1")
        self.assertEqual(StateStore(state_dir).load().project_id, "project-1")


    def test_non_git_is_rejected_without_initializing(self):
        root = Path(tempfile.mkdtemp())
        plan = root / "plan.json"
        plan.write_text('{"goal":"g","milestones":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Git repository"):
            initialize_project(root, plan, profile="adaptive", skill_path=ADAPTIVE_SKILL)
        self.assertFalse((root / ".git").exists())


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

    def test_arm_and_stop_hook_bind_the_initiating_thread_as_owner(self):
        """Владельцем становится ветка того самого Stop-события."""

        patch_hook_trust_gates(self)
        root = make_project()
        spawned: list[tuple] = []
        with isolated_launch_registry():
            arm(root)
            with mock.patch(
                "codex_autopilot.control.spawn_automatic_app_server_relay",
                side_effect=lambda project, **kw: spawned.append((project, kw)) or 42,
            ):
                output = handle_stop_hook(
                    {"cwd": str(root), "session_id": "session", "turn_id": "turn"}
                )
        self.assertTrue(spawned, "резервация не поднята")
        self.assertEqual(spawned[0][1]["initiator_thread_id"], "session")
        self.assertEqual(spawned[0][1]["initiator_turn_id"], "turn")
        self.assertTrue(output, "хук обязан отчитаться о запуске")
        self.assertFalse((root / ".codex-autopilot/launch-request.json").exists())
        state = StateStore(root / ".codex-autopilot").load()
        session = state.worker_sessions[-1]
        self.assertEqual(session["relay_owner_thread_id"], "session")

    def test_stale_stop_hook_arm_does_not_start_duplicate_dispatcher(self):
        root = make_project()
        with isolated_launch_registry():
            arm(root)
            store = StateStore(root / ".codex-autopilot")
            state = store.load()
            state.status = "RUNNING"
            state.phase = "RUNNING_TURN"
            state.dispatcher_pid = 123
            store.save(state)
            output = handle_stop_hook({"cwd": str(root), "session_id": "session", "turn_id": "turn"})
        self.assertEqual(output, {})
        import codex_autopilot.control as control

        self.assertFalse(hasattr(control, "spawn_dispatcher"))
        self.assertFalse((root / ".codex-autopilot/launch-request.json").exists())

    def test_stop_hook_claims_target_outside_initiating_cwd(self):
        """Инициирующая задача может стоять не в целевой папке."""

        patch_hook_trust_gates(self)
        root = make_project()
        outside = Path(tempfile.mkdtemp(prefix="codex-autopilot-initiator-outside-"))
        spawned: list[tuple] = []
        with isolated_launch_registry():
            arm(root)
            with mock.patch(
                "codex_autopilot.control.spawn_automatic_app_server_relay",
                side_effect=lambda project, **kw: spawned.append((project, kw)) or 43,
            ):
                output = handle_stop_hook(
                    {
                        "cwd": str(outside),
                        "session_id": "outside-session",
                        "turn_id": "outside-turn",
                    }
                )
        self.assertTrue(spawned, "цель вне инициирующей папки не поднята")
        self.assertEqual(spawned[0][0], root.resolve())
        self.assertEqual(spawned[0][1]["initiator_thread_id"], "outside-session")
        self.assertTrue(output, "хук обязан отчитаться о запуске")


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


if __name__ == "__main__":
    unittest.main()
