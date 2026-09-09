from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from .config import STATE_DIR_NAME, load_config
from .launch_registry import LaunchRegistry
from .run_state import StateStore, utc_now


def find_project_root(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / STATE_DIR_NAME / "config.toml").is_file():
            return candidate
    return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def arm(root: Path) -> None:
    cfg = load_config(root)
    store = StateStore(cfg.state_dir)
    state = store.load()
    if state.status == "RUNNING" and pid_alive(state.dispatcher_pid):
        raise RuntimeError(f"dispatcher is already running with pid {state.dispatcher_pid}")
    if state.status == "DONE":
        raise RuntimeError("the migrated or initialized roadmap is already DONE")
    payload = {
        "project_root": str(cfg.root),
        "armed_at": utc_now(),
        "run_id": state.run_id,
    }
    request_id = LaunchRegistry().add(payload)
    payload["request_id"] = request_id
    store.arm(payload)
    state.status = "READY"
    state.phase = "ARMED"
    store.save(state)


def spawn_dispatcher(root: Path, *, initiator_thread_id: str | None = None, initiator_turn_id: str | None = None) -> int:
    cfg = load_config(root)
    log_dir = cfg.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "dispatcher.log").open("a", encoding="utf-8")
    command = [sys.executable, "-m", "codex_autopilot.cli", "_dispatch", "--project", str(cfg.root)]
    if initiator_thread_id and initiator_turn_id:
        command.extend(["--initiator-thread", initiator_thread_id, "--initiator-turn", initiator_turn_id])
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    log.close()
    return proc.pid


def wait_for_dispatcher(root: Path, pid: int, timeout: float = 20) -> str:
    cfg = load_config(root)
    store = StateStore(cfg.state_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        state = store.load()
        if state.status == "BLOCKED":
            raise RuntimeError(f"dispatcher blocked during startup: {state.last_error or 'see BLOCKED.json'}")
        if state.dispatcher_pid == pid and state.phase in {"WAITING_INITIATOR", "PREPARING", "WAITING_RATE_LIMIT", "CREATING_THREAD", "THREAD_CREATED", "VERIFYING_MEMORY_MCP", "STARTING_TURN", "RUNNING_TURN"}:
            return state.phase
        time.sleep(0.1)
    raise RuntimeError(f"dispatcher {pid} did not become ready; see {cfg.state_dir / 'logs' / 'dispatcher.log'}")


def handle_stop_hook(payload: dict[str, Any]) -> dict[str, Any]:
    registry = LaunchRegistry()
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    store = StateStore(root / STATE_DIR_NAME) if root else None
    request = store.claim_launch() if store else None
    if request:
        registry.remove(str(request.get("request_id") or ""))
    else:
        request = registry.claim_unique(project_hint=root)
        if not request:
            return {}
        root = Path(str(request.get("project_root") or "")).expanduser().resolve()
        if not (root / STATE_DIR_NAME / "config.toml").is_file():
            raise RuntimeError(f"armed target is no longer an initialized Autopilot project: {root}")
        store = StateStore(root / STATE_DIR_NAME)
        local = store.claim_launch()
        if local and local.get("request_id") != request.get("request_id"):
            store.arm(local)
            raise RuntimeError("target project launch request did not match the initiating Stop hook")
    assert root is not None and store is not None
    state = store.load()
    if request.get("run_id") != state.run_id:
        raise RuntimeError("armed launch request belongs to an older Autopilot run")
    try:
        thread_id = str(payload["session_id"])
        turn_id = str(payload["turn_id"])
        pid = spawn_dispatcher(root, initiator_thread_id=thread_id, initiator_turn_id=turn_id)
        phase = wait_for_dispatcher(root, pid)
    except Exception:
        store.launch_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        registry.add(request)
        raise
    return {"continue": True, "systemMessage": f"Codex Autopilot dispatcher started (pid {pid}, {phase}). Worker 1 waits for this turn to complete."}


def _normalized_prompt(value: str) -> str:
    return " ".join(value.strip().lower().rstrip(".!?").split())


PAUSE_PROMPTS = {"pause codex autopilot", "stop codex autopilot", "приостанови codex autopilot", "останови codex autopilot"}
RESUME_PROMPTS = {"resume codex autopilot", "continue codex autopilot", "возобнови codex autopilot", "продолжи codex autopilot"}
STATUS_PROMPTS = {"codex autopilot status", "what is codex autopilot doing right now", "что сейчас делает codex autopilot", "статус codex autopilot"}
UNINSTALL_PROMPTS = {"uninstall codex autopilot", "remove codex autopilot", "удали codex autopilot"}


def handle_prompt_hook(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = _normalized_prompt(str(payload.get("prompt") or ""))
    if prompt not in PAUSE_PROMPTS | RESUME_PROMPTS | STATUS_PROMPTS | UNINSTALL_PROMPTS:
        return {}
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if prompt in UNINSTALL_PROMPTS:
        command = [sys.executable, "-m", "codex_autopilot.cli", "uninstall", "--yes"]
        if root:
            command.extend(["--project", str(root)])
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return {"decision": "block", "reason": "Codex Autopilot uninstall started. Project source and project state are preserved."}
    if not root:
        return {"decision": "block", "reason": "No initialized Codex Autopilot project was found from this working directory."}
    store = StateStore(root / STATE_DIR_NAME)
    state = store.load()
    if prompt in PAUSE_PROMPTS:
        store.request_pause()
        return {"decision": "block", "reason": "Pause requested. An active worker is interrupted and resume will use a fresh worker for the same milestone."}
    if prompt in RESUME_PROMPTS:
        if state.status == "DONE":
            return {"decision": "block", "reason": "Codex Autopilot is already DONE."}
        if state.status == "BLOCKED":
            return {"decision": "block", "reason": f"Codex Autopilot is BLOCKED: {state.last_error or 'review BLOCKED.json'}"}
        if pid_alive(state.dispatcher_pid):
            return {"decision": "block", "reason": f"Codex Autopilot is already running (pid {state.dispatcher_pid})."}
        store.clear_pause()
        pid = spawn_dispatcher(root)
        phase = wait_for_dispatcher(root, pid)
        return {"decision": "block", "reason": f"Codex Autopilot resumed (pid {pid}, {phase})."}
    return {"decision": "block", "reason": status_text(root)}


def status_text(root: Path) -> str:
    cfg = load_config(root)
    state = StateStore(cfg.state_dir).load()
    from .plan import load_plan
    plan = load_plan(cfg.state_dir, cfg.profile)
    running = pid_alive(state.dispatcher_pid)
    current = state.milestone_index + 1
    model = state.selected_model_display or "Host default"
    reasoning = state.selected_reasoning or "Host default"
    execution_mode = state.execution_mode or plan.milestones[state.milestone_index].execution_mode
    reason = state.model_selection_reason or plan.milestones[state.milestone_index].execution_mode_reason
    return (
        f"Codex Autopilot: milestone={current}/{len(plan.milestones)}, model={model}, "
        f"reasoning={reasoning}, execution_mode={execution_mode}, strategy={plan.model_strategy}, "
        f"status={state.status}, phase={state.phase}, worker={state.worker_sequence}, "
        f"dispatcher={'running' if running else 'not running'}, reason={reason}, "
        f"last_error={state.last_error or 'none'}"
    )
