from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from typing import Any, Callable

from .appserver import AppServerClient, AppServerError, AppServerRpcError, ApprovalRequired, PauseRequested, final_agent_message, is_rate_limit_error, rate_limit_reset_at
from .bootstrap import mark_roadmap, select_milestone
from .config import Config
from .models import ModelRoutingError, resolve_selection
from .plan import Plan, load_plan
from .reasoning import next_level
from .run_state import RunState, StateStore, TERMINAL_STATUSES, utc_now


class OrchestrationError(RuntimeError):
    pass


def parse_worker_status(message: str, adaptive: bool, allow_require_computer_use: bool = False) -> str:
    allowed = ["ROTATE", "DONE", "BLOCKED"] + (["ESCALATE"] if adaptive else []) + (["REQUIRE_COMPUTER_USE"] if allow_require_computer_use else [])
    pattern = re.compile(rf"(?m)^AUTOPILOT_STATUS:\s*({'|'.join(allowed)})\s*$")
    matches = pattern.findall(message)
    last = next((line.strip() for line in reversed(message.splitlines()) if line.strip()), "")
    if len(matches) != 1 or last != f"AUTOPILOT_STATUS: {matches[0]}":
        raise OrchestrationError("Worker final response must end with exactly one allowed AUTOPILOT_STATUS line")
    return matches[0]


def parse_computer_use_reason(message: str) -> str:
    matches = re.findall(r"(?m)^COMPUTER_USE_REASON:\s*(\S.*)\s*$", message)
    if len(matches) != 1:
        raise OrchestrationError("REQUIRE_COMPUTER_USE requires exactly one concrete COMPUTER_USE_REASON line")
    return matches[0].strip()


def _compact(path: Path, limit: int = 128_000) -> str:
    if not path.is_file():
        raise OrchestrationError(f"required handoff file is missing: {path}")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise OrchestrationError(f"required handoff file is empty: {path}")
    if len(text.encode()) > limit:
        raise OrchestrationError(f"handoff file exceeds {limit} bytes: {path}")
    return text


WORKER_WRITES = ("PROJECT_STATE.md", "HANDOFF.md")


def checkpoint_signature(cfg: Config) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in WORKER_WRITES:
        path = cfg.state_dir / name
        if path.exists():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def validate_checkpoint(cfg: Config, before: dict[str, str]) -> None:
    for name in WORKER_WRITES:
        path = cfg.state_dir / name
        _compact(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() == before.get(name):
            raise OrchestrationError(f"worker did not update required checkpoint file: {name}")
    _compact(cfg.state_dir / "DECISIONS.md")


def build_worker_prompt(cfg: Config, state: RunState, plan: Plan) -> str:
    milestone = _compact(cfg.state_dir / "MILESTONE.md")
    handoff = _compact(cfg.state_dir / "HANDOFF.md")
    project_state = _compact(cfg.state_dir / "PROJECT_STATE.md")
    decisions = _compact(cfg.state_dir / "DECISIONS.md")
    allow_require = cfg.adaptive and plan.model_strategy == "auto" and state.selected_model_key == "sol" and state.execution_mode == "code"
    statuses = ["ROTATE", "DONE", "BLOCKED"] + (["ESCALATE"] if cfg.adaptive else []) + (["REQUIRE_COMPUTER_USE"] if allow_require else [])
    status_lines = "\n".join(f"AUTOPILOT_STATUS: {value}" for value in statuses)
    reasoning = state.selected_reasoning if cfg.adaptive else "host default (no effort override)"
    return f"""Codex Autopilot v0.7 Desktop Native worker.

Run ID: {state.run_id}
Worker sequence: {state.worker_sequence}
Milestone index: {state.milestone_index + 1}/{len(plan.milestones)}
Attempt: {state.attempt}
Project root: {cfg.root}
Profile: {cfg.profile}
Model strategy: {plan.model_strategy}
Planned execution mode: {state.planned_execution_mode}
Effective execution mode: {state.execution_mode}
Selected model: {state.selected_model_display or 'host default (no model override)'}
Model selection reason: {state.model_selection_reason}
Effective reasoning: {reasoning}

A local non-AI dispatcher created this durable visible Codex task. Complete exactly
one milestone. Never create, fork, message, or start another Codex task. Never
operate the Codex UI. Respect every normal permission request; the dispatcher
does not approve actions.

Inspect the repository before trusting prose. Verify the Definition of Done.
Before your final response, replace PROJECT_STATE.md with concise factual current
state and replace HANDOFF.md with only what a fresh worker needs next. Update
DECISIONS.md only for durable decisions. Do not copy transcripts or reasoning.

ROTATE means this milestone is complete and another planned milestone remains.
DONE means the entire roadmap is complete. BLOCKED means progress requires user
input or an approval that cannot be completed in this turn.{chr(10) + 'ESCALATE means this milestone remains incomplete and should be retried in a fresh worker at the next reasoning level.' if cfg.adaptive else ''}
{('REQUIRE_COMPUTER_USE is allowed only when the Definition of Done truly cannot be completed without real GUI interaction. Complexity is not a reason. Put one line COMPUTER_USE_REASON: <specific GUI requirement> immediately before the final status.' if allow_require else '')}
{('This milestone requires Computer Use. Use that capability for the GUI portion of the Definition of Done, but never target the Codex UI itself.' if state.execution_mode == 'computer_use' else 'Use repository, code, shell, and non-GUI tools. Do not use Computer Use for this milestone.')}

End with exactly one of these lines and no text after it:
{status_lines}

## Current milestone
{milestone}

## Previous handoff
{handoff}

## Project state
{project_state}

## Durable decisions
{decisions}
"""


def _turn_by_client_id(thread: dict[str, Any], client_id: str | None) -> dict[str, Any] | None:
    turns = thread.get("turns") or []
    if client_id:
        for turn in reversed(turns):
            for item in turn.get("items") or []:
                if item.get("type") == "userMessage" and item.get("clientId") == client_id:
                    return turn
    return turns[-1] if turns else None


def _created_at(thread: dict[str, Any]) -> int:
    value = thread.get("createdAt")
    return value if isinstance(value, int) else 0


def match_saved_project(root: Path, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    resolved = root.resolve()
    matches: list[tuple[int, dict[str, Any]]] = []
    for project in projects:
        for entry in project.get("roots") or []:
            raw = entry.get("path")
            if not isinstance(raw, str):
                continue
            project_root = Path(raw).expanduser().resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue
            matches.append((len(project_root.parts), project))
    if not matches:
        return None
    longest = max(size for size, _ in matches)
    best = {project["id"]: project for size, project in matches if size == longest}
    if len(best) != 1:
        raise OrchestrationError("multiple saved Codex Projects match this path; set desktop.project_id")
    return next(iter(best.values()))


class DesktopOrchestrator:
    def __init__(self, cfg: Config, *, client_factory: Callable[..., Any] = AppServerClient, sleep_fn: Callable[[float], None] = time.sleep, now_fn: Callable[[], float] = time.time, emit: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.client_factory = client_factory
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        self.emit = emit or (lambda line: print(f"[Codex Autopilot] {line}", flush=True))
        self.store = StateStore(cfg.state_dir)
        self.client = None
        self.project_id: str | None = cfg.desktop.project_id
        self.plan = load_plan(cfg.state_dir, cfg.profile)

    def _event(self, method: str, params: dict[str, Any]) -> None:
        if method in {"turn/started", "turn/completed"}:
            self.emit(f"{method}: {(params.get('turn') or {}).get('id')}")

    def _new_client(self):
        return self.client_factory(self.cfg.desktop.binary, self.cfg.state_dir / "logs" / "app-server.jsonl", event_sink=self._event)

    def run(self, initiator_thread_id: str | None = None, initiator_turn_id: str | None = None) -> int:
        self._validate_project()
        self.store.acquire()
        try:
            state = self.store.load()
            if state.status == "DONE":
                self.emit("already complete")
                return 0
            self.store.clear_pause()
            self.client = self._new_client()
            initialized = self.client.connect()
            state.dispatcher_pid = os.getpid()
            state.initiator_thread_id = initiator_thread_id
            state.initiator_turn_id = initiator_turn_id
            if initiator_thread_id and initiator_turn_id:
                state.status = "RUNNING"
                state.phase = "WAITING_INITIATOR"
            self.store.save(state)
            self.emit(f"App Server ready: {initialized.get('userAgent', 'unknown')}")
            self._verify_permission_profile()
            self.project_id = self._resolve_project_id(state)
            state.project_id = self.project_id
            state.permission_profile = self.cfg.desktop.permission_profile
            self.store.save(state)
            if initiator_thread_id and initiator_turn_id:
                self._wait_for_initiator(state, initiator_thread_id, initiator_turn_id)
                state = self.store.load()
            if state.status in {"IDLE", "PAUSED"}:
                state.status = "RUNNING"
                state.phase = "PREPARING"
                self.store.save(state)
            elif state.status == "BLOCKED":
                self.emit("run is BLOCKED; start a new plan or resolve state before retrying")
                return 78
            elif state.phase not in {"PREPARING", "WAITING_RATE_LIMIT"}:
                state = self._reconcile(state)
            return self._loop(state)
        except PauseRequested:
            state = self.store.load()
            state.last_worker_status = "PAUSED"
            self._update_worker_history(state, status="PAUSED", completed_at=utc_now())
            self._retire_current_thread(state)
            state.status = "PAUSED"
            state.phase = "PAUSED"
            state.dispatcher_pid = None
            self.store.save(state)
            self.emit("paused; resume creates a fresh worker for the unfinished milestone")
            return 0
        except ApprovalRequired as exc:
            state = self.store.load()
            if state.current_thread_id and state.current_turn_id:
                try:
                    self.client.interrupt_turn(state.current_thread_id, state.current_turn_id)
                except AppServerError as interrupt_error:
                    self.emit(f"could not confirm worker interruption after approval request: {interrupt_error}")
            state.last_worker_status = "BLOCKED"
            self._update_worker_history(state, status="BLOCKED", completed_at=utc_now())
            self._retire_current_thread(state)
            return self._block(state, str(exc), approval=exc.payload)
        except (AppServerRpcError, AppServerError, OrchestrationError, ModelRoutingError) as exc:
            state = self.store.load()
            if is_rate_limit_error(getattr(exc, "error", None)):
                self._schedule_rate_limit(state, getattr(exc, "error", None))
                return self._loop(state)
            return self._block(state, str(exc))
        finally:
            if self.client:
                self.client.close()
            try:
                state = self.store.load()
                if state.dispatcher_pid == os.getpid():
                    state.dispatcher_pid = None
                    self.store.save(state)
            except Exception:
                pass
            self.store.release()

    def _validate_project(self) -> None:
        if not self.cfg.root.is_dir() or not (self.cfg.root / ".git").exists():
            raise OrchestrationError("project must be an existing Git repository")
        if not self.cfg.roadmap.is_file() or not self.cfg.skill_path.is_file():
            raise OrchestrationError("project roadmap or installed worker skill is missing")

    def _verify_permission_profile(self) -> None:
        profiles = self.client.list_permission_profiles(self.cfg.root)
        allowed = {entry.get("id") for entry in profiles if entry.get("allowed") is not False}
        if self.cfg.desktop.permission_profile not in allowed:
            raise OrchestrationError(f"permission profile {self.cfg.desktop.permission_profile!r} unavailable; allowed={sorted(str(x) for x in allowed)}")
        self.emit(f"permission profile={self.cfg.desktop.permission_profile}")

    def _resolve_project_id(self, state: RunState) -> str | None:
        configured = self.cfg.desktop.project_id or state.project_id
        if configured:
            project = self.client.read_project(configured)
            if match_saved_project(self.cfg.root, [project]) is None:
                raise OrchestrationError("configured saved Codex Project does not contain this path")
            self.emit(f"saved project={configured}")
            return configured
        selected = match_saved_project(self.cfg.root, self.client.list_projects())
        if selected:
            self.emit(f"saved project={selected['id']}")
            return selected["id"]
        self.emit("saved project=none; workers appear in Recents")
        return None

    def _wait_for_initiator(self, state: RunState, thread_id: str, turn_id: str) -> None:
        deadline = self.now_fn() + self.cfg.desktop.reconcile_timeout_seconds
        while self.now_fn() < deadline:
            if self.store.pause_requested():
                raise PauseRequested("Pause requested")
            try:
                thread = self.client.read_thread(thread_id)
            except AppServerRpcError:
                self.sleep_fn(0.25)
                continue
            target = next((turn for turn in thread.get("turns") or [] if turn.get("id") == turn_id), None)
            # While a synchronous Stop hook is running, a second App Server can
            # briefly observe this turn as interrupted. Only the durable
            # completed state opens the worker gate; every other state keeps it
            # closed until the reconciliation timeout.
            if target and target.get("status") == "completed":
                state.phase = "PREPARING"
                self.store.save(state)
                self.emit("initiating turn completed; Worker 1 may start")
                return
            self.sleep_fn(0.25)
        raise OrchestrationError("timed out waiting for durable completion of the initiating Codex turn")

    def _loop(self, state: RunState) -> int:
        while True:
            if state.status in TERMINAL_STATUSES:
                return 0 if state.status == "DONE" else 78
            if self.store.pause_requested():
                raise PauseRequested("Pause requested")
            if state.phase == "WAITING_RATE_LIMIT":
                self._wait_for_retry(state)
            state = self._run_worker(state)
            if state.status in {"DONE", "BLOCKED"}:
                return 0 if state.status == "DONE" else 78

    def _run_worker(self, state: RunState) -> RunState:
        milestone = self.plan.milestones[state.milestone_index]
        state.attempt += 1
        state.worker_sequence += 1
        state.current_thread_id = None
        state.current_turn_id = None
        state.client_user_message_id = str(uuid.uuid4())
        state.checkpoint_before = None
        state.milestone_id = milestone.id
        state.planned_execution_mode = milestone.execution_mode
        state.execution_mode = "computer_use" if state.capability_escalated else milestone.execution_mode
        state.selected_model_key = None
        state.selected_model_id = None
        state.selected_model_display = None
        state.model_selection_reason = milestone.execution_mode_reason
        state.reasoning_adjustment = None
        self.store.save(state)
        if self.cfg.adaptive:
            requested = state.selected_reasoning or milestone.reasoning or "medium"
            selection = resolve_selection(
                self.client.list_models(),
                strategy=self.plan.model_strategy,
                execution_mode=state.execution_mode,
                requested_reasoning=requested,
                execution_reason=state.capability_escalation_reason or milestone.execution_mode_reason,
            )
            state.selected_model_key = selection.key
            state.selected_model_id = selection.model_id
            state.selected_model_display = selection.display_name
            state.selected_reasoning = selection.reasoning
            state.model_selection_reason = selection.reason
            state.reasoning_adjustment = selection.reasoning_adjustment
            self.emit(f"model={selection.display_name} ({selection.model_id}); execution_mode={state.execution_mode}; reasoning={selection.reasoning}")
            self.emit(f"model reason={selection.reason}")
            if selection.reasoning_adjustment:
                self.emit(f"reasoning adjustment={selection.reasoning_adjustment}")
        else:
            state.selected_model_key = None
            state.selected_model_id = None
            state.selected_model_display = None
            state.selected_reasoning = None
            state.model_selection_reason = "Host Settings profile: dispatcher sends neither model nor effort."
            state.reasoning_adjustment = None
            self.emit(f"model=host default; execution_mode={state.execution_mode}; reasoning=host default; no model or effort field is sent")
        prompt = build_worker_prompt(self.cfg, state, self.plan)
        state.prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
        model_title = (state.selected_model_key or "Host").title()
        state.expected_thread_name = f"{self.cfg.desktop.title_prefix} · {milestone.id} · {model_title} · worker {state.worker_sequence} · {state.run_id[:8]}"
        state.creation_not_before = int(self.now_fn())
        state.phase = "CREATING_THREAD"
        self.store.save(state)
        started = self.client.start_thread(cwd=self.cfg.root, permission_profile=self.cfg.desktop.permission_profile, project_id=self.project_id, model=state.selected_model_id)
        state.current_thread_id = started["thread"]["id"]
        state.phase = "THREAD_CREATED"
        state.worker_history.append({
            "worker_sequence": state.worker_sequence,
            "milestone_id": state.milestone_id,
            "strategy": self.plan.model_strategy,
            "planned_execution_mode": state.planned_execution_mode,
            "execution_mode": state.execution_mode,
            "model_key": state.selected_model_key,
            "model_id": state.selected_model_id,
            "model_display": state.selected_model_display,
            "reasoning": state.selected_reasoning,
            "model_selection_reason": state.model_selection_reason,
            "reasoning_adjustment": state.reasoning_adjustment,
            "thread_id": state.current_thread_id,
            "turn_id": None,
            "status": "THREAD_CREATED",
            "started_at": utc_now(),
            "completed_at": None,
        })
        self.store.save(state)
        self._verify_thread_start(started, state)
        self.client.name_thread(state.current_thread_id, state.expected_thread_name)
        self.emit(f"Worker {state.worker_sequence} thread={state.current_thread_id}")
        return self._start_existing_thread(state, prompt, checkpoint_signature(self.cfg))

    def _start_existing_thread(self, state: RunState, prompt: str | None = None, before: dict[str, str] | None = None) -> RunState:
        if not state.current_thread_id or not state.client_user_message_id:
            raise OrchestrationError("cannot start recovered worker without durable IDs")
        prompt = prompt or build_worker_prompt(self.cfg, state, self.plan)
        if state.prompt_sha256 != hashlib.sha256(prompt.encode()).hexdigest():
            raise OrchestrationError("checkpoint changed during ambiguous turn start; refusing duplicate dispatch")
        before = before or state.checkpoint_before or checkpoint_signature(self.cfg)
        state.checkpoint_before = before
        state.phase = "STARTING_TURN"
        self.store.save(state)
        result = self.client.start_turn(thread_id=state.current_thread_id, prompt=prompt, effort=state.selected_reasoning, client_user_message_id=state.client_user_message_id, skill_name=self.cfg.skill_name, skill_path=self.cfg.skill_path)
        state.current_turn_id = result["turn"]["id"]
        state.phase = "RUNNING_TURN"
        self._update_worker_history(state, turn_id=state.current_turn_id, status="RUNNING")
        self.store.save(state)
        completed = self.client.wait_for_turn(state.current_thread_id, state.current_turn_id, timeout=self.cfg.desktop.turn_timeout_seconds, pause_requested=self.store.pause_requested)
        turn = dict(completed.turn)
        if not turn.get("error") and completed.errors:
            turn["error"] = (completed.errors[-1].get("error") or completed.errors[-1])
        return self._process_turn(state, turn, before)

    def _verify_thread_start(self, response: dict[str, Any], state: RunState) -> None:
        active = response.get("activePermissionProfile") or {}
        if active.get("id") != self.cfg.desktop.permission_profile:
            raise OrchestrationError("App Server did not apply :workspace")
        thread = response.get("thread") or {}
        if thread.get("cwd") and Path(thread["cwd"]).resolve() != self.cfg.root:
            raise OrchestrationError("App Server used an unexpected cwd")
        if self.project_id and thread.get("projectId") != self.project_id:
            raise OrchestrationError("App Server did not preserve projectId")
        if state.selected_model_id and response.get("model") != state.selected_model_id:
            raise OrchestrationError(f"App Server did not apply required model {state.selected_model_id}; no fallback was used")

    def _process_turn(self, state: RunState, turn: dict[str, Any], before: dict[str, str]) -> RunState:
        if turn.get("status") != "completed":
            error = turn.get("error") or {}
            if is_rate_limit_error(error):
                state.last_worker_status = "RATE_LIMITED"
                self._update_worker_history(state, status="RATE_LIMITED", completed_at=utc_now())
                self._retire_current_thread(state)
                self._schedule_rate_limit(state, error)
                return state
            state.last_worker_status = str(turn.get("status") or "FAILED").upper()
            self._update_worker_history(state, status=state.last_worker_status, completed_at=utc_now())
            self._block(state, f"worker turn ended with status={turn.get('status')!r}: {json.dumps(error, ensure_ascii=False)}")
            return state
        final = final_agent_message(turn)
        allow_require = self.cfg.adaptive and self.plan.model_strategy == "auto" and state.selected_model_key == "sol" and state.execution_mode == "code"
        worker_status = parse_worker_status(final, self.cfg.adaptive, allow_require)
        computer_use_reason = parse_computer_use_reason(final) if worker_status == "REQUIRE_COMPUTER_USE" else None
        validate_checkpoint(self.cfg, before)
        state.last_final_message = final
        state.last_worker_status = worker_status
        self._update_worker_history(state, status=worker_status, completed_at=utc_now())
        self.emit(f"Worker {state.worker_sequence} completed with {worker_status}")
        if self.cfg.auto_commit:
            self._git_checkpoint(state, worker_status)
        state.checkpoint_before = None
        self._retire_current_thread(state)
        state.retry_count = 0
        state.retry_at = None
        state.reset_at = None
        state.last_error = None
        if worker_status in {"ROTATE", "DONE"}:
            completed = state.milestone_index + 1
            mark_roadmap(self.cfg.root, self.plan, completed)
        if worker_status == "ROTATE":
            if state.milestone_index + 1 >= len(self.plan.milestones):
                self._block(state, "last planned milestone returned ROTATE instead of DONE")
                return state
            state.milestone_index += 1
            state.selected_reasoning = None
            state.selected_model_key = None
            state.selected_model_id = None
            state.selected_model_display = None
            state.model_selection_reason = None
            state.reasoning_adjustment = None
            state.capability_escalated = False
            state.capability_escalation_reason = None
            state.attempt = 0
            select_milestone(self.cfg.state_dir, self.plan, state.milestone_index)
            state.phase = "PREPARING"
            self.store.save(state)
            return state
        if worker_status == "REQUIRE_COMPUTER_USE":
            state.capability_escalated = True
            state.capability_escalation_reason = computer_use_reason
            state.selected_model_key = None
            state.selected_model_id = None
            state.selected_model_display = None
            state.model_selection_reason = None
            state.phase = "PREPARING"
            self.store.save(state)
            return state
        if worker_status == "ESCALATE":
            higher = next_level(state.selected_reasoning or "medium")
            if higher is None:
                self._block(state, "worker requested ESCALATE at max; user input is required")
                return state
            state.selected_reasoning = higher
            state.selected_model_key = None
            state.selected_model_id = None
            state.selected_model_display = None
            state.phase = "PREPARING"
            self.store.save(state)
            return state
        state.status = worker_status
        state.phase = worker_status
        state.completed_at = utc_now()
        self.store.save(state)
        return state

    def _update_worker_history(self, state: RunState, **updates: object) -> None:
        if not state.worker_history:
            return
        current = state.worker_history[-1]
        if current.get("worker_sequence") != state.worker_sequence:
            return
        current.update(updates)

    def _retire_current_thread(self, state: RunState) -> None:
        if state.current_thread_id and state.current_thread_id not in state.previous_thread_ids:
            state.previous_thread_ids.append(state.current_thread_id)
        state.current_thread_id = None
        state.current_turn_id = None
        self.store.save(state)

    def _schedule_rate_limit(self, state: RunState, error: Any) -> None:
        state.retry_count += 1
        if state.retry_count > self.cfg.retry.maximum_attempts:
            self._block(state, "rate-limit retry budget exhausted")
            return
        reset_at = None
        try:
            reset_at = rate_limit_reset_at(self.client.rate_limits())
        except Exception as exc:
            self.emit(f"rate-limit reset time unavailable: {exc}")
        backoff = min(self.cfg.retry.initial_seconds * (2 ** min(state.retry_count - 1, 20)), self.cfg.retry.maximum_seconds)
        now = int(self.now_fn())
        state.reset_at = reset_at
        state.retry_at = max(now + backoff, (reset_at + 5) if reset_at else now + backoff)
        state.phase = "WAITING_RATE_LIMIT"
        state.status = "RUNNING"
        state.last_error = json.dumps(error, ensure_ascii=False)
        self.store.save(state)
        self.emit(f"rate limited; retry scheduled for {state.retry_at}")

    def _wait_for_retry(self, state: RunState) -> None:
        while state.retry_at and self.now_fn() < state.retry_at:
            if self.store.pause_requested():
                raise PauseRequested("Pause requested")
            self.sleep_fn(min(30, max(1, state.retry_at - self.now_fn())))
        try:
            reset = rate_limit_reset_at(self.client.rate_limits())
            if reset and reset > self.now_fn():
                state.retry_at = reset + 5
                state.reset_at = reset
                self.store.save(state)
                return self._wait_for_retry(state)
        except Exception as exc:
            self.emit(f"rate-limit recheck unavailable; retrying: {exc}")
        state.phase = "PREPARING"
        self.store.save(state)

    def _reconcile(self, state: RunState) -> RunState:
        self.emit(f"recovering phase={state.phase}")
        if state.phase == "WAITING_RATE_LIMIT":
            return state
        if state.phase == "CREATING_THREAD":
            candidates = self.client.list_threads(self.cfg.root, state.expected_thread_name)
            if not candidates:
                candidates = [item for item in self.client.list_threads(self.cfg.root) if _created_at(item) >= (state.creation_not_before or 0) and item.get("name") in {None, state.expected_thread_name}]
            if len(candidates) > 1:
                self._block(state, "ambiguous recovery: multiple possible worker threads")
                return state
            if len(candidates) == 1:
                state.current_thread_id = candidates[0]["id"]
                state.phase = "THREAD_CREATED"
                self.store.save(state)
            else:
                state.phase = "PREPARING"
                self.store.save(state)
                return state
        if state.phase in {"THREAD_CREATED", "STARTING_TURN", "RUNNING_TURN"}:
            if not state.current_thread_id:
                self._block(state, "recovery lacks current_thread_id")
                return state
            thread = self.client.read_thread(state.current_thread_id)
            turn = _turn_by_client_id(thread, state.client_user_message_id)
            if turn is None:
                if state.phase == "RUNNING_TURN":
                    self._block(state, "recovery cannot find the persisted worker turn")
                    return state
                state.phase = "THREAD_CREATED"
                self.store.save(state)
                return self._start_existing_thread(state)
            state.current_turn_id = turn.get("id")
            self.store.save(state)
            if turn.get("status") in {"completed", "failed", "interrupted"}:
                return self._process_turn(state, turn, state.checkpoint_before or {})
            deadline = self.now_fn() + self.cfg.desktop.reconcile_timeout_seconds
            while self.now_fn() < deadline:
                if self.store.pause_requested():
                    raise PauseRequested("Pause requested")
                self.sleep_fn(5)
                turn = _turn_by_client_id(self.client.read_thread(state.current_thread_id), state.client_user_message_id)
                if turn and turn.get("status") in {"completed", "failed", "interrupted"}:
                    return self._process_turn(state, turn, state.checkpoint_before or {})
            self._block(state, "existing worker still appears active; stopped to prevent overlap")
        return state

    def _git_checkpoint(self, state: RunState, worker_status: str) -> None:
        add = subprocess.run(["git", "-C", str(self.cfg.root), "add", "-A"], capture_output=True, text=True)
        if add.returncode:
            raise OrchestrationError(f"git add failed: {add.stderr.strip()}")
        if subprocess.run(["git", "-C", str(self.cfg.root), "diff", "--cached", "--quiet"]).returncode == 0:
            return
        commit = subprocess.run(["git", "-C", str(self.cfg.root), "commit", "-m", f"Codex Autopilot: M{state.milestone_index + 1} {worker_status}"], capture_output=True, text=True)
        if commit.returncode:
            raise OrchestrationError("auto_commit was explicitly enabled but commit failed; Git config was not changed: " + commit.stderr.strip())

    def _block(self, state: RunState, reason: str, *, approval: dict[str, Any] | None = None) -> int:
        state.status = "BLOCKED"
        state.phase = "BLOCKED"
        state.last_error = reason
        state.completed_at = utc_now()
        self.store.save(state)
        (self.cfg.state_dir / "BLOCKED.json").write_text(json.dumps({"at": utc_now(), "reason": reason, "approval_request": approval}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.emit(f"BLOCKED: {reason}")
        return 78


def state_summary(state: RunState) -> str:
    data = asdict(state)
    data.pop("last_final_message", None)
    return json.dumps(data, ensure_ascii=False, indent=2)
