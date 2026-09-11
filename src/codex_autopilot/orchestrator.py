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
from .config import Config, HEADLESS_APP_SERVER_SURFACE
from .language import is_russian
from .memory import MemoryError as ProjectMemoryError, MemoryValidationError, ProjectMemory
from .models import ModelRoutingError, resolve_selection
from .plan import Plan, load_plan
from .preflight import MEMORY_SERVER_NAME, REQUIRED_MEMORY_TOOLS, installed_plugin_id, installed_plugin_root
from .project_association import ProjectAssociationError, match_saved_project as _match_saved_project
from .reasoning import next_level
from .run_state import RunState, StateStore, TERMINAL_STATUSES, utc_now
from .task_state import TaskState, dependencies_eligible, transition_task
from .thread_titles import implementation_thread_title


class OrchestrationError(RuntimeError):
    pass


class ProjectSlotWriterBusy(OrchestrationError):
    pass


WORKSPACE_HANDOFF_OK = "AUTOPILOT_WORKSPACE_READY"
WORKSPACE_HANDOFF_PROMPT = (
    "Codex Autopilot workspace handoff. Do not inspect or modify files and do not call tools. "
    f"Reply exactly: {WORKSPACE_HANDOFF_OK}"
)


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


WORKER_WRITES = ("HANDOFF.md",)


def checkpoint_signature(cfg: Config) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in WORKER_WRITES:
        path = cfg.state_dir / name
        if path.exists():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def interrupted_turn_is_pristine(
    cfg: Config,
    turn: dict[str, Any],
    before: dict[str, str],
    *,
    memory: ProjectMemory,
    memory_audit_before: int,
) -> bool:
    """Return true only when an interrupted worker provably performed no work."""
    harmless_item_types = {"userMessage", "reasoning"}
    items = turn.get("items") or []
    if any(str(item.get("type") or "") not in harmless_item_types for item in items):
        return False
    return checkpoint_signature(cfg) == before and memory.audit_highwater() == memory_audit_before


def validate_checkpoint(
    cfg: Config,
    before: dict[str, str],
    *,
    memory: ProjectMemory,
    milestone_id: str,
    memory_audit_before: int,
    require_completion_evidence: bool,
) -> list[dict[str, Any]]:
    for name in WORKER_WRITES:
        path = cfg.state_dir / name
        _compact(path, 8_192)
        if hashlib.sha256(path.read_bytes()).hexdigest() == before.get(name):
            raise OrchestrationError(f"worker did not update required checkpoint file: {name}")
    if not require_completion_evidence:
        return []
    evidence = memory.milestone_evidence(milestone_id, after_audit_id=memory_audit_before)
    if not evidence:
        raise OrchestrationError(
            f"{milestone_id} returned completion without new Project Memory evidence; "
            "AUTOPILOT_STATUS alone is insufficient"
        )
    return evidence


def _bootstrap_memory(cfg: Config, plan: Plan, state: RunState) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    memory = ProjectMemory(cfg.root)
    milestone = plan.milestones[state.milestone_index]
    query = " ".join([milestone.title, milestone.objective, *milestone.definition_of_done])
    return memory.critical_constraints(8), memory.context_ids(query, 8)


def build_worker_prompt(cfg: Config, state: RunState, plan: Plan) -> str:
    milestone = _compact(cfg.state_dir / "MILESTONE.md")
    handoff = _compact(cfg.state_dir / "HANDOFF.md", 8_192)
    constraints, relevant = _bootstrap_memory(cfg, plan, state)
    russian = is_russian(cfg.language)
    constraint_text = "\n".join(
        f"- {item['id']} [{item['origin']}/{item['status']}]: {str(item['statement'])[:600]}"
        for item in constraints
    ) or ("- Ничего не зафиксировано." if russian else "- None recorded.")
    relevant_text = "\n".join(
        f"- {item['id']} [{item['category']}/{item['status']}]"
        for item in relevant
    ) or (
        "- Ничего не выбрано. При необходимости выполни поиск."
        if russian
        else "- None selected. Search on demand."
    )
    allow_require = cfg.adaptive and plan.model_strategy == "auto" and state.selected_model_key == "sol" and state.execution_mode == "code"
    statuses = ["ROTATE", "DONE", "BLOCKED"] + (["ESCALATE"] if cfg.adaptive else []) + (["REQUIRE_COMPUTER_USE"] if allow_require else [])
    status_lines = "\n".join(f"AUTOPILOT_STATUS: {value}" for value in statuses)
    reasoning = state.selected_reasoning if cfg.adaptive else "host default (no effort override)"
    if russian:
        return f"""Codex Autopilot v0.8 Desktop Native worker.

Язык ответов: русский (ru). Пиши на русском все сообщения пользователю,
промежуточные обновления, пояснения и финальный отчёт, даже если исходный текст
задачи написан на другом языке. Не переводи имена файлов, идентификаторы, код,
названия инструментов и машинные строки протокола `AUTOPILOT_STATUS`.

ID запуска: {state.run_id}
Номер worker: {state.worker_sequence}
Номер задачи: {state.milestone_index + 1}/{len(plan.milestones)}
Попытка: {state.attempt}
Корень проекта: {cfg.root}
Профиль: {cfg.profile}
Стратегия модели: {plan.model_strategy}
Плановый режим выполнения: {state.planned_execution_mode}
Фактический режим выполнения: {state.execution_mode}
Выбранная модель: {state.selected_model_display or 'настройка хоста по умолчанию (без переопределения модели)'}
Причина выбора модели: {state.model_selection_reason}
Уровень рассуждения: {reasoning}

Локальный диспетчер без ИИ создал эту постоянную видимую задачу Codex. Выполни
ровно одну текущую задачу. Не создавай, не форкай, не запускай другие задачи
Codex и не отправляй в них сообщения. Не управляй интерфейсом Codex. Соблюдай
все обычные запросы разрешений; диспетчер не подтверждает действия за тебя.

Сначала проверь репозиторий и только затем доверяй описаниям. Проверь каждый
критерий готовности. Project Memory — каноническое хранилище знаний. HANDOFF.md —
лишь соседняя записка между worker'ами, а не доказательство и не Truth. Прежде
чем опираться на важное историческое утверждение, запроси встроенный MCP Project
Memory. Не превращай Observation в Truth или Agent Decision в User Constraint.
NO EVIDENCE -> NO TRUTH.

Используй разрешённый MCP-инструмент `memory`: `operation=search` или
`operation=get` для чтения; `operation=record_evidence` для фактических
доказательств из файлов, тестов, сборки, инструментов, пользователя или среды;
`operation=record_verified_fact` только с существующими evidence ID;
`operation=add_observation` для гипотез. Не смешивай желаемые Decisions и
фактические Truth.

Перед ROTATE или DONE проверь каждый критерий готовности, выполни необходимую
верификацию и запиши хотя бы одно новое доказательство для задачи
`{state.milestone_id}`. Диспетчер отклонит маркер завершения без нового evidence.
Обнови HANDOFF.md только разделами Completed, Changed, Risks,
Relevant memory IDs и Next. Размер — не более 8 КиБ; не копируй туда тела
записей, транскрипты или рассуждения. Не изменяй PROJECT_STATE.md и DECISIONS.md:
диспетчер формирует их из канонической памяти.

ROTATE означает: текущая задача завершена, но в плане остались другие.
DONE означает: завершена вся дорожная карта.
BLOCKED означает: нужен ввод пользователя или разрешение, которое нельзя
получить в этом ходе.{chr(10) + 'ESCALATE означает: текущая задача не завершена и должна быть повторена свежим worker на следующем уровне рассуждения.' if cfg.adaptive else ''}
{('REQUIRE_COMPUTER_USE допустим только тогда, когда критерии готовности действительно требуют GUI. Сложность не является причиной. Непосредственно перед финальным статусом добавь одну строку COMPUTER_USE_REASON: <конкретная причина необходимости GUI>.' if allow_require else '')}
{('Эта задача требует Computer Use. Используй его для GUI-части критериев готовности, но никогда не управляй самим интерфейсом Codex.' if state.execution_mode == 'computer_use' else 'Используй репозиторий, код, shell и инструменты без GUI. Для этой задачи не используй Computer Use.')}

Заверши ровно одной из следующих строк; после неё не должно быть текста:
{status_lines}

## Текущая задача
{milestone}

## Общая цель
{plan.goal}

## Критические ограничения (ограниченная выборка канонических записей)
{constraint_text}

## Релевантные ID памяти (проверь перед использованием)
{relevant_text}

## Предыдущая передача контекста (только справочно)
{handoff}
"""
    language_notice = (
        f"Response language: {cfg.language}. Write every user-facing update, explanation, "
        "and final report in that language even if the source task is in another language. "
        "Keep code, identifiers, tool names, and AUTOPILOT_STATUS protocol lines exact.\n\n"
    )
    return f"""Codex Autopilot v0.8 Desktop Native worker.

{language_notice}Run ID: {state.run_id}
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
Project Memory is the canonical knowledge store. HANDOFF.md is only an adjacent
worker note and never evidence or Truth. Before relying on an important historical
claim, query the built-in Project Memory MCP. Never infer Observation -> Truth or
Agent Decision -> User Constraint. NO EVIDENCE -> NO TRUTH.

Use the allowlisted `memory` MCP tool. Pass `operation=search` or `operation=get`
for retrieval. Record actual file/test/build/tool/user/environment evidence with
`operation=record_evidence`; create Truth only with
`operation=record_verified_fact` and existing evidence IDs; record hypotheses
with `operation=add_observation`.
Keep desired Decisions and actual-state Truth distinct.

Before ROTATE or DONE, evaluate every DoD item, perform the required verification,
and record at least one new evidence item linked to milestone `{state.milestone_id}`.
The dispatcher rejects a completion marker without new milestone evidence. Update
HANDOFF.md using only: Completed, Changed, Risks, Relevant memory IDs, and Next.
Keep it under 8 KiB; do not copy record bodies, transcripts, or reasoning. Do not
edit PROJECT_STATE.md or DECISIONS.md; the dispatcher renders those human views
from canonical memory.

ROTATE means this milestone is complete and another planned milestone remains.
DONE means the entire roadmap is complete. BLOCKED means progress requires user
input or an approval that cannot be completed in this turn.{chr(10) + 'ESCALATE means this milestone remains incomplete and should be retried in a fresh worker at the next reasoning level.' if cfg.adaptive else ''}
{('REQUIRE_COMPUTER_USE is allowed only when the Definition of Done truly cannot be completed without real GUI interaction. Complexity is not a reason. Put one line COMPUTER_USE_REASON: <specific GUI requirement> immediately before the final status.' if allow_require else '')}
{('This milestone requires Computer Use. Use that capability for the GUI portion of the Definition of Done, but never target the Codex UI itself.' if state.execution_mode == 'computer_use' else 'Use repository, code, shell, and non-GUI tools. Do not use Computer Use for this milestone.')}

End with exactly one of these lines and no text after it:
{status_lines}

## Current milestone
{milestone}

## Global goal
{plan.goal}

## Critical constraints (bounded canonical records)
{constraint_text}

## Relevant memory IDs (query before relying)
{relevant_text}

## Previous adjacent handoff (advisory only)
{handoff}
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
    try:
        return _match_saved_project(root, projects)
    except ProjectAssociationError as exc:
        raise OrchestrationError(f"{exc}; set desktop.project_id") from exc


class HeadlessAppServerOrchestrator:
    """Historical v0.8 stdio runner with no Desktop interactivity promise."""
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
        self.memory = ProjectMemory(cfg.root)

    def _event(self, method: str, params: dict[str, Any]) -> None:
        if method in {"turn/started", "turn/completed"}:
            self.emit(f"{method}: {(params.get('turn') or {}).get('id')}")

    def _new_client(self):
        return self.client_factory(self.cfg.desktop.binary, self.cfg.state_dir / "logs" / "app-server.jsonl", event_sink=self._event)

    def run(self, initiator_thread_id: str | None = None, initiator_turn_id: str | None = None) -> int:
        if self.cfg.runtime.worker_surface != HEADLESS_APP_SERVER_SURFACE:
            raise OrchestrationError(
                "external App Server production is forbidden for desktop_owned runs"
            )
        self.store.acquire()
        try:
            self._validate_project()
            state = self.store.load()
            if state.status == "DONE":
                self.emit("already complete")
                return 0
            self.store.clear_pause()
            state.status = "RUNNING"
            if state.phase in {"IDLE", "ARMED", "PREFLIGHT_PASSED", "PAUSED"}:
                state.phase = "CONNECTING_APP_SERVER"
            state.dispatcher_pid = os.getpid()
            state.initiator_thread_id = initiator_thread_id
            state.initiator_turn_id = initiator_turn_id
            self.store.save(state)
            self.client = self._new_client()
            initialized = self.client.connect()
            if initiator_thread_id and initiator_turn_id:
                state.phase = "WAITING_INITIATOR"
            self.store.save(state)
            self.emit(f"App Server ready: {initialized.get('userAgent', 'unknown')}")
            self._verify_permission_profile()
            self.project_id = self._resolve_project_id(state)
            state.project_id = self.project_id
            state.desktop_project_id = self.cfg.desktop.desktop_project_id
            state.permission_profile = self.cfg.desktop.permission_profile
            self.store.save(state)
            if initiator_thread_id and initiator_turn_id:
                self._wait_for_initiator(state, initiator_thread_id, initiator_turn_id)
                state = self.store.load()
            if state.status in {"IDLE", "READY", "PAUSED", "RUNNING"} and state.phase in {"IDLE", "ARMED", "PREFLIGHT_PASSED", "CONNECTING_APP_SERVER", "WAITING_INITIATOR", "PAUSED"}:
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
            self._retry_graph_task(state)
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
        except ProjectSlotWriterBusy as exc:
            state = self.store.load()
            state.worker_slot_cursor = max(0, state.worker_slot_cursor - 1)
            state.worker_sequence = max(0, state.worker_sequence - 1)
            state.attempt = max(0, state.attempt - 1)
            state.current_thread_id = None
            state.current_turn_id = None
            state.status = "WAITING"
            state.phase = "WAITING_PROJECT_SLOT_RELEASE"
            state.last_error = str(exc)
            state.completed_at = None
            self.store.save(state)
            self.emit(f"waiting for Desktop to release the current project slot: {exc}")
            return 0
        except (AppServerRpcError, AppServerError, OrchestrationError, ModelRoutingError, ProjectMemoryError, MemoryValidationError) as exc:
            state = self.store.load()
            if is_rate_limit_error(getattr(exc, "error", None)):
                self._retry_graph_task(state)
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
        health = self.memory.ensure_healthy(recover=True)
        if health == "recovered":
            self.emit("Project Memory recovered from the latest verified-milestone backup")
        if not self.cfg.memory_database.is_file():
            raise OrchestrationError("Project Memory database is missing")
        if self.plan.execution_strategy != "serial" or self.plan.max_parallel_workers != 1:
            raise OrchestrationError(
                "controller-owned App Server restoration currently requires a serial plan"
            )
        unsupported = [
            task.id
            for task in self.plan.tasks
            if task.verification.required and task.verification.policy != "self"
        ]
        if unsupported:
            raise OrchestrationError(
                "controller-owned App Server restoration requires self verification; "
                f"unsupported tasks={unsupported}"
            )

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
            if str(project.get("id")) != configured:
                raise OrchestrationError("App Server returned an unexpected configured Codex Project")
            self.emit(f"App Server project={configured}; cwd={self.cfg.root}")
            return configured
        selected = match_saved_project(self.cfg.root, self.client.list_projects())
        if selected:
            self.emit(f"saved project={selected['id']}")
            return selected["id"]
        self.emit("App Server project=none; worker task remains bound to the canonical cwd")
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
        slot_release_deadline: float | None = None
        while True:
            if state.status in TERMINAL_STATUSES:
                return 0 if state.status == "DONE" else 78
            if self.store.pause_requested():
                raise PauseRequested("Pause requested")
            if state.phase == "WAITING_RATE_LIMIT":
                self._wait_for_retry(state)
            try:
                state = self._run_worker(state)
                slot_release_deadline = None
            except ProjectSlotWriterBusy as exc:
                state.worker_slot_cursor = max(0, state.worker_slot_cursor - 1)
                state.worker_sequence = max(0, state.worker_sequence - 1)
                state.attempt = max(0, state.attempt - 1)
                state.current_thread_id = None
                state.current_turn_id = None
                state.status = "RUNNING"
                state.phase = "WAITING_PROJECT_SLOT_RELEASE"
                state.last_error = str(exc)
                state.completed_at = None
                self.store.save(state)
                if slot_release_deadline is None:
                    slot_release_deadline = (
                        self.now_fn() + self.cfg.desktop.reconcile_timeout_seconds
                    )
                    self.emit(
                        "Desktop still owns the reserved project slot; waiting for writer "
                        f"release without creating another task: {exc}"
                    )
                if self.now_fn() >= slot_release_deadline:
                    state.status = "WAITING"
                    self.store.save(state)
                    self.emit(
                        "timed out waiting for Desktop writer release; the same slot is "
                        "preserved for a later resume"
                    )
                    return 0
                self.sleep_fn(min(1.0, max(0.05, slot_release_deadline - self.now_fn())))
                state.phase = "PREPARING"
                self.store.save(state)
                continue
            if state.status == "WAITING":
                return 0
            if state.status in {"DONE", "BLOCKED"}:
                return 0 if state.status == "DONE" else 78

    def _activate_graph_task(self, state: RunState, task_id: str) -> None:
        if not state.task_states:
            return
        current = TaskState(state.task_states[task_id])
        if current is TaskState.RETRY_WAIT:
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.READY
            )
            current = TaskState.READY
        elif current is TaskState.WAITING:
            if not dependencies_eligible(self.plan, task_id, state.task_states):
                raise OrchestrationError(
                    f"task {task_id} is not dependency-eligible for App Server dispatch"
                )
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.READY
            )
            current = TaskState.READY
        if current is not TaskState.READY:
            raise OrchestrationError(
                f"task {task_id} cannot start from durable state {current.value}"
            )
        if state.active_task_ids:
            raise OrchestrationError(
                f"serial App Server dispatch found active tasks {state.active_task_ids}"
            )
        state.task_states = transition_task(
            self.plan, state.task_states, task_id, TaskState.RUNNING
        )
        state.active_task_ids = [task_id]
        state.task_attempts[task_id] = int(state.task_attempts.get(task_id, 0)) + 1
        state.task_ready_since.pop(task_id, None)

    def _retry_graph_task(self, state: RunState) -> None:
        task_id = state.milestone_id
        if not state.task_states or not task_id:
            return
        current = TaskState(state.task_states[task_id])
        if current in {TaskState.RUNNING, TaskState.VERIFYING, TaskState.REVISING}:
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.RETRY_WAIT
            )
        state.active_task_ids = [
            value for value in state.active_task_ids if value != task_id
        ]
        state.task_ready_since.pop(task_id, None)

    def _complete_graph_task(self, state: RunState) -> None:
        task_id = state.milestone_id
        if not state.task_states or not task_id:
            return
        if TaskState(state.task_states[task_id]) in {
            TaskState.WAITING,
            TaskState.READY,
            TaskState.RETRY_WAIT,
        }:
            # Compatibility with a v0.8 checkpoint written before graph state
            # was synchronized with an already-started App Server turn.
            self._activate_graph_task(state, task_id)
        state.task_states = transition_task(
            self.plan, state.task_states, task_id, TaskState.IMPLEMENTED
        )
        task = self.plan.task_map[task_id]
        if task.verification.required:
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.VERIFYING
            )
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.VERIFIED
            )
        else:
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.VERIFIED
            )
        state.active_task_ids = [
            value for value in state.active_task_ids if value != task_id
        ]

    def _block_graph_task(self, state: RunState) -> None:
        task_id = state.milestone_id
        if not state.task_states or not task_id or task_id not in state.task_states:
            return
        current = TaskState(state.task_states[task_id])
        if current not in {TaskState.VERIFIED, TaskState.BLOCKED, TaskState.CANCELLED}:
            state.task_states = transition_task(
                self.plan, state.task_states, task_id, TaskState.BLOCKED
            )
        state.active_task_ids = [
            value for value in state.active_task_ids if value != task_id
        ]

    def _run_worker(self, state: RunState) -> RunState:
        milestone = self.plan.milestones[state.milestone_index]
        self._activate_graph_task(state, milestone.id)
        state.attempt += 1
        state.worker_sequence += 1
        state.current_thread_id = None
        state.current_turn_id = None
        state.client_user_message_id = str(uuid.uuid4())
        state.checkpoint_before = None
        state.memory_audit_before = self.memory.audit_highwater()
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
        state.prompt_chars = len(prompt)
        state.prompt_approx_tokens = (len(prompt) + 3) // 4
        state.memory_records_at_start = self.memory.record_count()
        state.relevant_memory_count = len(_bootstrap_memory(self.cfg, self.plan, state)[1])
        state.expected_thread_name = implementation_thread_title(
            milestone.id,
            milestone.title,
            role_name=self.plan.role_map[milestone.role].name,
        )
        state.creation_not_before = int(self.now_fn())
        state.phase = "CREATING_THREAD"
        self.store.save(state)
        started = self._claim_or_create_thread(state)
        state.current_thread_id = str(started["thread"]["id"])
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
            "prompt_chars": state.prompt_chars,
            "prompt_approx_tokens": state.prompt_approx_tokens,
            "memory_records_at_start": state.memory_records_at_start,
            "relevant_memory_count": state.relevant_memory_count,
            "thread_id": state.current_thread_id,
            "turn_id": None,
            "status": "THREAD_CREATED",
            "started_at": utc_now(),
            "completed_at": None,
        })
        self.store.save(state)
        self._verify_thread_start(started, state)
        state.phase = "VERIFYING_MEMORY_MCP"
        self.store.save(state)
        self._verify_memory_mcp(state.current_thread_id)
        self.client.name_thread(state.current_thread_id, state.expected_thread_name)
        self.emit(f"Worker {state.worker_sequence} thread={state.current_thread_id}")
        return self._start_existing_thread(state, prompt, checkpoint_signature(self.cfg))

    def _claim_or_create_thread(self, state: RunState) -> dict[str, Any]:
        return self.client.start_thread(
            cwd=self.cfg.root,
            permission_profile=self.cfg.desktop.permission_profile,
            project_id=self.project_id,
            model=state.selected_model_id,
            plugin_root=installed_plugin_root(self.cfg.skill_path),
        )

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
        turn_args: dict[str, Any] = {
            "thread_id": state.current_thread_id,
            "prompt": prompt,
            "effort": state.selected_reasoning,
            "client_user_message_id": state.client_user_message_id,
            "skill_name": self.cfg.skill_name,
            "skill_path": self.cfg.skill_path,
            "cwd": self.cfg.root,
        }
        result = self.client.start_turn(**turn_args)
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

    def _verify_memory_mcp(self, thread_id: str) -> None:
        servers = self.client.list_mcp_server_status(thread_id)
        server = next((item for item in servers if item.get("name") == MEMORY_SERVER_NAME), None)
        if not server:
            raise OrchestrationError("built-in Project Memory MCP is missing from the worker thread")
        if server.get("runtimeStatus") != "connected":
            raise OrchestrationError(f"built-in Project Memory MCP is not connected: {server.get('runtimeStatus')}")
        expected_plugin_id = installed_plugin_id(installed_plugin_root(self.cfg.skill_path))
        if server.get("pluginId") != expected_plugin_id:
            raise OrchestrationError("built-in Project Memory MCP lost installed-plugin provenance")
        missing = REQUIRED_MEMORY_TOOLS - set((server.get("tools") or {}).keys())
        if missing:
            raise OrchestrationError(f"built-in Project Memory MCP is missing tools: {sorted(missing)}")
        identity = self.client.call_mcp_tool(thread_id, MEMORY_SERVER_NAME, "memory", {"operation": "current"}).get("structuredContent") or {}
        if Path(str(identity.get("project_root") or "")).resolve() != self.cfg.root or identity.get("initialized") is not True:
            raise OrchestrationError("built-in Project Memory MCP is not bound to this initialized target project")
        self.emit("Project Memory MCP connected and project-scoped")

    def _process_turn(self, state: RunState, turn: dict[str, Any], before: dict[str, str]) -> RunState:
        if turn.get("status") != "completed":
            error = turn.get("error") or {}
            if is_rate_limit_error(error):
                state.last_worker_status = "RATE_LIMITED"
                self._update_worker_history(state, status="RATE_LIMITED", completed_at=utc_now())
                self._retire_current_thread(state)
                self._retry_graph_task(state)
                self._schedule_rate_limit(state, error)
                return state
            if turn.get("status") == "interrupted" and interrupted_turn_is_pristine(
                self.cfg,
                turn,
                before,
                memory=self.memory,
                memory_audit_before=state.memory_audit_before or 0,
            ):
                state.last_worker_status = "INTERRUPTED_NO_CHANGES"
                self._update_worker_history(
                    state,
                    status=state.last_worker_status,
                    completed_at=utc_now(),
                )
                self._retire_current_thread(state)
                self._retry_graph_task(state)
                state.checkpoint_before = None
                state.memory_audit_before = None
                state.last_error = None
                state.status = "RUNNING"
                state.phase = "PREPARING"
                self.store.save(state)
                self.emit("interrupted worker had no tool activity or durable changes; retrying the same milestone in a fresh worker")
                return state
            state.last_worker_status = str(turn.get("status") or "FAILED").upper()
            self._update_worker_history(state, status=state.last_worker_status, completed_at=utc_now())
            self._block(state, f"worker turn ended with status={turn.get('status')!r}: {json.dumps(error, ensure_ascii=False)}")
            return state
        final = final_agent_message(turn)
        allow_require = self.cfg.adaptive and self.plan.model_strategy == "auto" and state.selected_model_key == "sol" and state.execution_mode == "code"
        worker_status = parse_worker_status(final, self.cfg.adaptive, allow_require)
        computer_use_reason = parse_computer_use_reason(final) if worker_status == "REQUIRE_COMPUTER_USE" else None
        completion_evidence = validate_checkpoint(
            self.cfg,
            before,
            memory=self.memory,
            milestone_id=state.milestone_id or self.plan.milestones[state.milestone_index].id,
            memory_audit_before=state.memory_audit_before or 0,
            require_completion_evidence=worker_status in {"ROTATE", "DONE"},
        )
        state.last_final_message = final
        state.last_worker_status = worker_status
        self._update_worker_history(
            state,
            status=worker_status,
            completed_at=utc_now(),
            evidence_ids=[item["id"] for item in completion_evidence],
        )
        self.emit(f"Worker {state.worker_sequence} completed with {worker_status}")
        if worker_status == "DONE" and state.milestone_index + 1 < len(self.plan.milestones):
            self._block(state, "non-final task returned DONE instead of ROTATE")
            return state
        if self.cfg.auto_commit:
            self._git_checkpoint(state, worker_status)
        state.checkpoint_before = None
        state.memory_audit_before = None
        self._retire_current_thread(state)
        state.retry_count = 0
        state.retry_at = None
        state.reset_at = None
        state.last_error = None
        if worker_status in {"ROTATE", "DONE"}:
            self._complete_graph_task(state)
            self.memory.mark_milestone_complete(
                milestone_id=state.milestone_id or self.plan.milestones[state.milestone_index].id,
                run_id=state.run_id,
                worker_sequence=state.worker_sequence,
            )
            completed = state.milestone_index + 1
            mark_roadmap(
                self.cfg.root,
                self.plan,
                completed,
                language=self.cfg.language,
            )
        self.memory.render_views()
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
            select_milestone(
                self.cfg.state_dir,
                self.plan,
                state.milestone_index,
                language=self.cfg.language,
            )
            state.phase = "PREPARING"
            self.store.save(state)
            return state
        if worker_status == "REQUIRE_COMPUTER_USE":
            self._retry_graph_task(state)
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
            self._retry_graph_task(state)
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
        thread_id = state.current_thread_id
        if thread_id and thread_id not in state.previous_thread_ids:
            state.previous_thread_ids.append(thread_id)
        if thread_id and self.client and hasattr(self.client, "unsubscribe_thread"):
            try:
                self.client.unsubscribe_thread(thread_id)
            except AppServerError as exc:
                self.emit(f"could not release worker task subscription immediately: {exc}")
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
        if state.phase in {"WAITING_PROJECT_SLOT", "WAITING_PROJECT_SLOT_RELEASE"}:
            if state.worker_slot_cursor >= len(self.cfg.desktop.worker_thread_ids):
                state.status = "WAITING"
                self.store.save(state)
                return state
            state.status = "RUNNING"
            state.phase = "PREPARING"
            self.store.save(state)
            return state
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
        if state.phase in {"CLAIMING_PROJECT_SLOT", "PREPARING_PROJECT_SLOT"}:
            if not state.current_thread_id:
                self._block(state, "project-slot recovery lacks current_thread_id")
                return state
            started = self.client.resume_thread(state.current_thread_id)
            thread = started.get("thread") or {}
            needs_handoff = Path(str(thread.get("cwd") or "")).resolve() != self.cfg.root
            if state.selected_model_id and thread.get("model") != state.selected_model_id:
                needs_handoff = True
            if needs_handoff:
                handoff = self.client.start_plain_turn(
                    thread_id=state.current_thread_id,
                    prompt=WORKSPACE_HANDOFF_PROMPT,
                    effort=state.selected_reasoning,
                    client_user_message_id=str(uuid.uuid4()),
                    cwd=self.cfg.root,
                    permission_profile=self.cfg.desktop.permission_profile,
                    model=state.selected_model_id,
                )
                completed = self.client.wait_for_turn(
                    state.current_thread_id,
                    handoff["turn"]["id"],
                    timeout=self.cfg.desktop.reconcile_timeout_seconds,
                    pause_requested=self.store.pause_requested,
                )
                if completed.turn.get("status") != "completed" or final_agent_message(completed.turn).strip() != WORKSPACE_HANDOFF_OK:
                    self._block(state, "project-slot recovery workspace handoff failed")
                    return state
            state.phase = "VERIFYING_MEMORY_MCP"
            self.store.save(state)
            self._verify_memory_mcp(state.current_thread_id)
            self.client.name_thread(state.current_thread_id, state.expected_thread_name)
            return self._start_existing_thread(state)
        if state.phase in {"THREAD_CREATED", "VERIFYING_MEMORY_MCP", "STARTING_TURN", "RUNNING_TURN"}:
            if not state.current_thread_id:
                self._block(state, "recovery lacks current_thread_id")
                return state
            thread = self.client.read_thread(state.current_thread_id)
            turn = _turn_by_client_id(thread, state.client_user_message_id)
            if turn is None:
                if state.phase == "RUNNING_TURN":
                    self._block(state, "recovery cannot find the persisted worker turn")
                    return state
                state.phase = "VERIFYING_MEMORY_MCP"
                self.store.save(state)
                self._verify_memory_mcp(state.current_thread_id)
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
        self._block_graph_task(state)
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


# Compatibility alias for v0.8 imports. New callers should name the explicit
# headless boundary rather than implying that App Server owns a Desktop task.
DesktopOrchestrator = HeadlessAppServerOrchestrator
