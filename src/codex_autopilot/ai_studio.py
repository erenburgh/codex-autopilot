from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .language import is_russian
from .memory import MemoryValidationError, ProjectMemory
from .models import MODEL_IDS, MODEL_LABELS, logical_model
from .pipeline_engineer import (
    FORBIDDEN_ACTIONS,
    INFRASTRUCTURE_INCIDENT_CLASSES,
    IncidentClass,
    IncidentPhase,
)
from .rules import rules_for_prompt
from .plan import Plan, RoleProfile, Task
from .task_state import dependency_state_satisfies
from .verification import VerificationIssue, verifier_route


PHASES = frozenset({"implementation", "verification", "revision", "planning", "replanning"})
HARD_MAX_MEMORY_RECORDS = 20
HARD_MAX_DEPENDENCY_OUTPUTS = 20
MAX_MEMORY_STATEMENT_CHARS = 800
MAX_OUTPUT_EXCERPT_CHARS = 2_000
MAX_PROMPT_CHARS = 64_000

PIPELINE_ENGINEER_SYSTEM_ROLE = RoleProfile(
    id="pipeline-engineer",
    name="Pipeline Engineer · On call",
    responsibilities=(
        "Diagnose and recover Codex Autopilot infrastructure incidents only.",
        "Use the incident package and its bounded allowlisted runbook.",
        "Require a passing healthcheck before affected tasks resume.",
    ),
    domain_focus=("pipeline", "runtime", "integration", "tooling"),
    preferred_tools=("bounded local diagnostics", "durable incident journal"),
    context_priorities=("system state", "recent events", "allowed and forbidden actions"),
    verification_expectations=(
        "Record every recovery action and prove the declared healthcheck passed.",
    ),
)


class ContextBoundaryError(RuntimeError):
    """A selective context request is invalid or exceeds a hard runtime bound."""


@dataclass(frozen=True, slots=True)
class RuntimeRoute:
    role_id: str
    execution_mode: str
    model_key: str | None
    model_id: str | None
    model_display: str
    reasoning: str | None


@dataclass(frozen=True, slots=True)
class SelectiveContext:
    memory_queries: tuple[str, ...]
    verified_state: tuple[dict[str, Any], ...]
    dependency_outputs: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_queries": list(self.memory_queries),
            "verified_state": list(self.verified_state),
            "dependency_outputs": list(self.dependency_outputs),
        }


class AIStudioRuntime:
    """Stateless role/task runtime for fresh Codex workers.

    The object owns immutable plan/configuration references only. It deliberately
    has no thread, turn, transcript, conversation, or worker-session collection.
    Every prompt is rebuilt from the canonical task graph, an explicit task-state
    snapshot, Project Memory selectors, and phase-specific structured inputs.
    """

    __slots__ = ("plan", "project_root", "language", "skill_path", "memory", "state_dir")

    def __init__(
        self,
        plan: Plan,
        project_root: Path,
        *,
        language: str,
        skill_path: Path,
        memory: ProjectMemory | None = None,
    ) -> None:
        self.plan = plan
        self.project_root = project_root.expanduser().resolve()
        self.language = language
        self.skill_path = skill_path.expanduser().resolve()
        self.memory = memory or ProjectMemory(self.project_root)
        # Каталог состояния нужен только для истории нарушений правил:
        # чаще нарушавшиеся идут в контексте выше (R17).
        self.state_dir = self.project_root / ".codex-autopilot"

    def route(self, task_id: str, *, phase: str = "implementation") -> RuntimeRoute:
        """Route solely from capability/model strategy, never from role identity."""

        self._phase(phase)
        task = self._task(task_id)
        if phase == "verification":
            selected = verifier_route(self.plan, task)
            return RuntimeRoute(
                role_id=selected.role_id,
                execution_mode=selected.execution_mode,
                model_key=selected.model_key,
                model_id=selected.model_id,
                model_display=selected.model_display,
                reasoning=selected.reasoning,
            )
        execution_mode = task.execution_mode
        if self.plan.model_strategy == "host-settings":
            return RuntimeRoute(
                role_id=task.role,
                execution_mode=execution_mode,
                model_key=None,
                model_id=None,
                model_display="Host settings",
                reasoning=None,
            )
        model_key = logical_model(self.plan.model_strategy, execution_mode)
        return RuntimeRoute(
            role_id=task.role,
            execution_mode=execution_mode,
            model_key=model_key,
            model_id=MODEL_IDS[model_key],
            model_display=MODEL_LABELS[model_key],
            reasoning=task.reasoning or "medium",
        )

    @staticmethod
    def system_roles() -> tuple[RoleProfile, ...]:
        """Return permanent Studio roles; planner-defined roles remain separate."""

        return (PIPELINE_ENGINEER_SYSTEM_ROLE,)

    def build_pipeline_engineer_prompt(
        self,
        incident_package: Mapping[str, Any],
        *,
        reservation_token: str,
    ) -> str:
        """Build a fresh, infrastructure-only on-call prompt.

        The deterministic supervisor must first transition an incident to
        PIPELINE_ENGINEER. Ordinary tasks and production-quality failures cannot
        use this entry point to manufacture a privileged specialist.
        """

        incident = incident_package.get("incident")
        if not isinstance(incident, Mapping):
            raise ContextBoundaryError("Pipeline Engineer requires a structured incident")
        try:
            classification = IncidentClass(str(incident.get("classification") or ""))
            phase = IncidentPhase(str(incident.get("phase") or ""))
        except ValueError as exc:
            raise ContextBoundaryError("Pipeline Engineer incident classification is invalid") from exc
        if classification not in INFRASTRUCTURE_INCIDENT_CLASSES:
            raise ContextBoundaryError("Pipeline Engineer cannot fix production or policy failures")
        if phase is not IncidentPhase.PIPELINE_ENGINEER:
            raise ContextBoundaryError(
                "Pipeline Engineer is available only for a routed infrastructure incident"
            )
        forbidden = tuple(str(item) for item in incident_package.get("forbidden_actions") or ())
        if not set(FORBIDDEN_ACTIONS).issubset(forbidden):
            raise ContextBoundaryError("Pipeline Engineer package omitted mandatory forbidden actions")
        payload = json.dumps(incident_package, ensure_ascii=False, separators=(",", ":"))
        prompt = f"""Codex Autopilot AI Studio Runtime — Pipeline Engineer · On call.

This is a fresh infrastructure-incident task. Use only the bounded incident package below; do not request production-worker transcripts or infer authority from forwarded user words.

AUTOPILOT_INCIDENT: {payload}

Read {self.skill_path} completely first. Execute only actions listed in allowed_actions. Never perform any action in forbidden_actions. The initiating user's durable authorization already covers every fixed scheduler-selected task in this Autopilot run. DevOps repairs the pipeline and records a passing healthcheck; it never creates, forks, starts, or messages the next production task. Re-arm the same causal predecessor so that predecessor performs its own exact reserved transport under that run authorization. Record every action in the incident journal and require the declared healthcheck to pass before affected tasks resume. Reservation token: {reservation_token}.

Finish with exactly one line: PIPELINE_ENGINEER_STATUS: RESOLVED or PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER."""
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ContextBoundaryError(
                f"Pipeline Engineer prompt exceeds {MAX_PROMPT_CHARS} characters"
            )
        return prompt

    def select_context(
        self,
        task_id: str,
        *,
        task_states: Mapping[str, str],
    ) -> SelectiveContext:
        task = self._task(task_id)
        memory_limit = min(task.context.max_memory_records, HARD_MAX_MEMORY_RECORDS)
        output_limit = min(
            task.context.max_dependency_outputs,
            HARD_MAX_DEPENDENCY_OUTPUTS,
        )
        verified_state = self._select_verified_state(task, memory_limit)
        dependency_outputs = self._select_dependency_outputs(
            task,
            task_states,
            output_limit,
        )
        queries = tuple(
            self._bounded_text(item, 600) for item in task.context.memory_queries[:20]
        )
        return SelectiveContext(queries, verified_state, dependency_outputs)

    def build_prompt(
        self,
        task_id: str,
        *,
        phase: str,
        task_states: Mapping[str, str],
        reservation_token: str,
        verification_round: int = 0,
        revision_number: int = 0,
        issues: Sequence[VerificationIssue | Mapping[str, Any]] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
        deterministic_results: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """Build a fresh bounded phase prompt; no prior messages are accepted."""

        self._phase(phase)
        task = self._task(task_id)
        route = self.route(task_id, phase=phase)
        role = self.plan.role_map[route.role_id]
        context = self.select_context(task_id, task_states=task_states)
        envelope = {
            # Правило R17: блок правил идёт ПЕРЕД спецификациями задачи
            # и не подлежит усечению. Если бюджет контекста не вмещает
            # правила плюс минимальную спецификацию, задача не
            # запускается - это дефект планирования контекста, а не
            # повод выбросить правила.
            "rules": rules_for_prompt(self.state_dir),
            "phase": phase,
            "task": self._task_contract(task),
            "role": self._role_contract(role),
            "definition_of_done": list(task.definition_of_done),
            "acceptance_gate": {
                "original_user_request": self.plan.user_request,
                "run_goal": self.plan.goal,
                "task_definition_of_done": list(task.definition_of_done),
                "implementation_tests_are_evidence_only": True,
            },
            "resources": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "target": item.target,
                    "access": item.access,
                    **({"description": item.description} if item.description else {}),
                }
                for item in task.resources
            ],
            "context": context.to_dict(),
            "verification": self._verification_contract(task),
            "phase_input": {
                **({"verification_round": verification_round} if verification_round else {}),
                **({"revision_number": revision_number} if revision_number else {}),
                **({"issues": [self._issue(item) for item in issues]} if issues else {}),
                **(
                    {"implementation_evidence": self._evidence_selectors(evidence)}
                    if evidence
                    else {}
                ),
                **(
                    {"deterministic_results": [dict(item) for item in deterministic_results]}
                    if deterministic_results
                    else {}
                ),
            },
            "route": {
                "execution_mode": route.execution_mode,
                "role_id": route.role_id,
            },
        }
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        prompt = self._render_prompt(
            task,
            phase=phase,
            payload=payload,
            reservation_token=reservation_token,
            verification_round=verification_round,
            revision_number=revision_number,
        )
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ContextBoundaryError(
                f"{phase} prompt for {task_id} exceeds {MAX_PROMPT_CHARS} characters; "
                "the rules block is not truncatable, so this is a context-planning "
                "defect: narrow the task context instead"
            )
        return prompt

    def _select_verified_state(
        self,
        task: Task,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(raw: Mapping[str, Any]) -> None:
            record_id = str(raw.get("id") or "")
            if not record_id or record_id in seen or not self._context_eligible(raw):
                return
            selected.append(self._memory_selector(raw))
            seen.add(record_id)

        for record_id in task.context.memory_record_ids:
            if len(selected) >= limit:
                break
            try:
                add(self.memory.get_record(record_id))
            except MemoryValidationError as exc:
                raise ContextBoundaryError(
                    f"task {task.id} references unavailable Project Memory record {record_id!r}"
                ) from exc

        for query in task.context.memory_queries:
            if len(selected) >= limit:
                break
            remaining = min(limit - len(selected), HARD_MAX_MEMORY_RECORDS)
            try:
                page = self.memory.search(
                    query=query,
                    categories=["truth", "decision", "constraint"],
                    limit=remaining,
                )
            except MemoryValidationError as exc:
                raise ContextBoundaryError(
                    f"task {task.id} has an invalid Project Memory query"
                ) from exc
            for record in page.records:
                add(record)
                if len(selected) >= limit:
                    break
        return tuple(selected)

    def _select_dependency_outputs(
        self,
        task: Task,
        task_states: Mapping[str, str],
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()
        selected: list[dict[str, Any]] = []
        for dependency_id in task.context.dependency_outputs:
            if len(selected) >= limit:
                break
            dependency = self.plan.task_map[dependency_id]
            raw_state = task_states.get(dependency_id)
            if raw_state is None or not dependency_state_satisfies(dependency, raw_state):
                raise ContextBoundaryError(
                    f"task {task.id} requested output from unverified dependency {dependency_id}"
                )
            evidence = self.memory.milestone_evidence(dependency_id, limit=20)
            evidence_ids = [str(item["id"]) for item in evidence[:8]]
            outputs = dependency.outputs or (None,)
            for output in outputs:
                if len(selected) >= limit:
                    break
                item: dict[str, Any] = {
                    "dependency_task_id": dependency_id,
                    "dependency_state": str(raw_state),
                    "evidence_ids": evidence_ids,
                }
                if output is None:
                    item.update(
                        {
                            "output_id": "verified-completion",
                            "description": "Verified dependency completion and linked evidence.",
                            "required": True,
                        }
                    )
                else:
                    item.update(
                        {
                            "output_id": output.id,
                            "description": self._bounded_text(output.description, 800),
                            "required": output.required,
                        }
                    )
                    if output.path:
                        item.update(self._output_file_context(output.path))
                selected.append(item)
        return tuple(selected)

    def _output_file_context(self, raw_path: str) -> dict[str, Any]:
        candidate = (self.project_root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise ContextBoundaryError("dependency output path escapes the project root") from exc
        result: dict[str, Any] = {"path": str(relative), "exists": candidate.is_file()}
        if not candidate.is_file():
            return result
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        result.update({"size_bytes": candidate.stat().st_size, "sha256": digest.hexdigest()})
        try:
            with candidate.open("r", encoding="utf-8", errors="strict") as handle:
                text = handle.read(MAX_OUTPUT_EXCERPT_CHARS + 1)
        except UnicodeDecodeError:
            return result
        result["content_excerpt"] = text[:MAX_OUTPUT_EXCERPT_CHARS]
        result["truncated"] = len(text) > MAX_OUTPUT_EXCERPT_CHARS
        return result

    @staticmethod
    def _context_eligible(raw: Mapping[str, Any]) -> bool:
        return (str(raw.get("category")), str(raw.get("status"))) in {
            ("truth", "verified"),
            ("decision", "accepted"),
            ("constraint", "active"),
        }

    @classmethod
    def _memory_selector(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        evidence = raw.get("evidence")
        evidence_ids = (
            [str(item.get("id")) for item in evidence if isinstance(item, Mapping) and item.get("id")]
            if isinstance(evidence, list)
            else []
        )
        return {
            "id": str(raw["id"]),
            "category": str(raw["category"]),
            "status": str(raw["status"]),
            "origin": str(raw.get("origin") or ""),
            "statement": cls._bounded_text(str(raw.get("statement") or ""), MAX_MEMORY_STATEMENT_CHARS),
            **({"evidence_ids": evidence_ids[:8]} if evidence_ids else {}),
        }

    @staticmethod
    def _evidence_selectors(evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        selectors: list[dict[str, Any]] = []
        for item in evidence[:20]:
            selector = {
                key: item[key]
                for key in ("id", "kind", "role", "path", "artifact_path")
                if item.get(key) is not None
            }
            if selector.get("id"):
                selectors.append(selector)
        return selectors

    @staticmethod
    def _task_contract(task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "objective": task.objective,
            "role_id": task.role,
            "depends_on": list(task.depends_on),
            "priority": task.priority,
            "execution_mode": task.execution_mode,
            "execution_mode_reason": task.execution_mode_reason,
            "required_capabilities": list(task.required_capabilities),
            "tags": list(task.tags),
            "outputs": [
                {
                    "id": item.id,
                    "description": item.description,
                    **({"path": item.path} if item.path else {}),
                    "required": item.required,
                }
                for item in task.outputs
            ],
        }

    @staticmethod
    def _role_contract(role: RoleProfile) -> dict[str, Any]:
        return {
            "id": role.id,
            "name": role.name,
            "responsibilities": list(role.responsibilities),
            "domain_focus": list(role.domain_focus),
            "preferred_tools": list(role.preferred_tools),
            "context_priorities": list(role.context_priorities),
            "verification_expectations": list(role.verification_expectations),
        }

    @staticmethod
    def _verification_contract(task: Task) -> dict[str, Any]:
        policy = task.verification
        return {
            "policy": policy.policy,
            "required": policy.required,
            "deterministic_checks": [
                {
                    "id": check.id,
                    "kind": check.kind,
                    "description": check.description,
                    **({"argv": list(check.argv)} if check.argv else {}),
                    **({"path": check.path} if check.path else {}),
                    "timeout_seconds": check.timeout_seconds,
                    "expected_exit_code": check.expected_exit_code,
                }
                for check in policy.deterministic_checks
            ],
            "max_revision_attempts": policy.max_revision_attempts,
        }

    @staticmethod
    def _issue(raw: VerificationIssue | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(raw, VerificationIssue):
            return raw.to_dict()
        return dict(raw)

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def _render_prompt(
        self,
        task: Task,
        *,
        phase: str,
        payload: str,
        reservation_token: str,
        verification_round: int,
        revision_number: int,
    ) -> str:
        russian = is_russian(self.language)
        if phase == "verification":
            identity = (
                f"свежий независимый verifier V{verification_round}"
                if russian
                else f"fresh independent verifier V{verification_round}"
            )
            finish = (
                'Независимо сопоставь результат с acceptance_gate.original_user_request, '
                'run_goal, структурированным контрактом задачи и каждым пунктом DoD. Тесты, '
                'написанные implementer, — только evidence: они не определяют и не отменяют '
                'критерии приёмки. PASS допустим только после этой независимой проверки. Запиши '
                'новое evidence с role=independent_verification. Последняя непустая строка: '
                'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]} или REVISE с непустым '
                'массивом структурированных issues.'
                if russian
                else 'Independently compare the result with '
                'acceptance_gate.original_user_request, run_goal, the structured task contract, '
                'and every DoD item. Implementer-authored tests are evidence only: they neither '
                'define nor waive acceptance criteria. PASS is allowed only after this independent '
                'check. Record new evidence with role=independent_verification. Final non-empty line: '
                'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]} or REVISE with a non-empty '
                'structured issues array.'
            )
        elif phase == "revision":
            identity = f"свежий revision worker R{revision_number}" if russian else f"fresh revision worker R{revision_number}"
            finish = (
                f"Запиши новое проверяемое evidence для {task.id}; для evidence checks используй точный check ID как role. Перед финальной строкой дай строку AUTOPILOT_RULES с id правил из блока rules, которые ты применила к этой задаче (например AUTOPILOT_RULES: R7, R17). Заверши ровно одной строкой AUTOPILOT_STATUS: ROTATE, BLOCKED или ESCALATE. Если обнаружена необходимая смена prerequisite/dependency/resource/verification контракта, вместо status верни ровно одну финальную строку PLAN_CHANGE_REQUEST с JSON-полями request_version=1, kind, target_task_id={task.id}, summary, rationale, change и evidence_ids."
                if russian
                else f"Record new verifiable evidence for {task.id}; use each exact check ID as the role for evidence checks. Before the final line, give an AUTOPILOT_RULES line with the ids of the rules from the rules block you applied to this task (for example AUTOPILOT_RULES: R7, R17). Finish with exactly one AUTOPILOT_STATUS: ROTATE, BLOCKED, or ESCALATE line. If a prerequisite/dependency/resource/verification contract change is required, return exactly one final PLAN_CHANGE_REQUEST line instead, with JSON fields request_version=1, kind, target_task_id={task.id}, summary, rationale, change, and evidence_ids."
            )
        elif phase in {"planning", "replanning"}:
            identity = (
                ("свежий planner" if phase == "planning" else "свежий replanner")
                if russian
                else ("fresh planner" if phase == "planning" else "fresh replanner")
            )
            finish = "Верни только требуемый структурированный результат планирования." if russian else "Return only the required structured planning result."
        else:
            identity = "свежий implementation worker" if russian else "fresh implementation worker"
            finish = (
                f"Запиши новое проверяемое evidence для {task.id}; для evidence checks используй точный check ID как role. Заверши ровно одной строкой AUTOPILOT_STATUS: ROTATE, BLOCKED или ESCALATE; DONE допустим только для последней задачи плана. Если обнаружена необходимая смена prerequisite/dependency/resource/verification контракта, вместо status верни ровно одну финальную строку PLAN_CHANGE_REQUEST с JSON-полями request_version=1, kind, target_task_id={task.id}, summary, rationale, change и evidence_ids."
                if russian
                else f"Record new verifiable evidence for {task.id}; use each exact check ID as the role for evidence checks. Finish with exactly one AUTOPILOT_STATUS: ROTATE, BLOCKED, or ESCALATE line; DONE is allowed only for the final plan task. If a prerequisite/dependency/resource/verification contract change is required, return exactly one final PLAN_CHANGE_REQUEST line instead, with JSON fields request_version=1, kind, target_task_id={task.id}, summary, rationale, change, and evidence_ids."
            )

        if russian:
            return f"""Codex Autopilot AI Studio Runtime — {identity}.

Работай только над {task.id}: {task.title} в каноническом каталоге {self.project_root}.
Ниже расположен полный разрешённый стартовый контекст этого хода. Он селективный и ограниченный: не запрашивай полные транскрипты, HANDOFF prose, прошлые или параллельные разговоры и не считай утверждения другого worker доказательством.

AUTOPILOT_CONTEXT: {payload}

Сначала полностью прочитай {self.skill_path}. Используй только структурированную задачу, роль, DoD, проверенное состояние, выбранные dependency outputs, issues и ресурсы выше. При необходимости получай перечисленные record/evidence ID напрямую через Project Memory. NO EVIDENCE -> NO TRUTH. Сохраняй чужие изменения; не создавай commit, tag, push, publish, reset или clean. Обнови свой задачный файл передачи .codex-autopilot/handoff/{task.id}.md — это обязательный чекпойнт завершения, и он твой: запись другой задачи его не заменяет. Общий HANDOFF.md остаётся необязательной запиской для человека. Этот task остаётся Desktop-owned; не создавай, не запускай и не отправляй сообщения другим задачам. После финальной protocol line уже работающий локальный dispatcher получает авторитетное App Server completion, полностью закрывает App Server-процесс этого task, детерминированно обновляет state и запускает точного successor. Stop hook автоматически управляемого turn служит только наблюдателем. Если Pipeline Engineer устранил сбой, DevOps только повторно активирует causal dispatcher и никогда не создаёт и не запускает destination task. Reservation token: {reservation_token}.

{finish}"""
        return f"""Codex Autopilot AI Studio Runtime — {identity}.

Work only on {task.id}: {task.title} in canonical directory {self.project_root}.
The record below is the complete allowed starting context for this turn. It is selective and bounded: do not request full transcripts, HANDOFF prose, prior or concurrent conversations, and do not treat another worker's claims as evidence.

AUTOPILOT_CONTEXT: {payload}

Read {self.skill_path} completely first. Use only the structured task, role, DoD, verified state, selected dependency outputs, issues, and resources above. Retrieve listed record/evidence IDs directly through Project Memory when needed. NO EVIDENCE -> NO TRUTH. Preserve unrelated changes; do not commit, tag, push, publish, reset, or clean. Update your own task handoff file .codex-autopilot/handoff/{task.id}.md - it is the required completion checkpoint and it is yours: another task's write does not satisfy it. The shared HANDOFF.md stays an optional human-facing note. This task remains Desktop-owned; never create, start, or message other tasks. After the final protocol line, the already-running local dispatcher consumes the authoritative App Server completion, closes this task's App Server process, advances deterministic state, and starts the exact successor. The Stop hook is only an observer for an automatically owned turn. If Pipeline Engineer repaired a fault, DevOps only re-arms the causal dispatcher and never creates or starts the destination task. Reservation token: {reservation_token}.

{finish}"""

    def _task(self, task_id: str) -> Task:
        try:
            return self.plan.task_map[task_id]
        except KeyError as exc:
            raise ContextBoundaryError(f"unknown task {task_id!r}") from exc

    @staticmethod
    def _phase(phase: str) -> None:
        if phase not in PHASES:
            raise ContextBoundaryError(f"unsupported AI Studio phase: {phase}")
