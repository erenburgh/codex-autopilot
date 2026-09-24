from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

from . import __version__
from .artifact_staging import resolve_canonical_project_root
from .config import STATE_DIR_NAME
from .department_acceptance import RESERVED_TOOL_PREFIX, store_department_rubric
from .department_runtime import authorize_rubric_proposal
from .memory import CATEGORIES, MAX_PAGE_SIZE, MemoryError, MemoryValidationError, ProjectMemory


JSON_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": False}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {**JSON_OBJECT, "properties": properties, "required": required or []}


_ACTION_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "memory_current",
        "description": (
            "Get this task's contract, the original user request, critical constraints, "
            "and bounded relevant memory IDs. Pass task_id - it is in your prompt - so the "
            "answer describes your own task; on a task graph it cannot be inferred. Pass "
            "expect_user_request_sha256 exactly as your prompt supplies it: the runtime "
            "then verifies the request for you and fails closed if the text changed, so "
            "you never hash anything yourself."
        ),
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "expect_user_request_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                },
            }
        ),
    },
    {
        "name": "memory_search",
        "description": "Search bounded project memory with category filters and cursor pagination.",
        "inputSchema": _schema(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "categories": {"type": "array", "items": {"type": "string", "enum": sorted(CATEGORIES)}, "minItems": 1, "uniqueItems": True},
                "scope": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
                "cursor": {"type": "string", "maxLength": 512},
            },
            ["query"],
        ),
    },
    {
        "name": "memory_get",
        "description": "Get one record, evidence item, verification result, or conflict with its provenance trail.",
        "inputSchema": _schema({"id": {"type": "string", "pattern": "^[A-Z]+-[0-9]{3,}$"}}, ["id"]),
    },
    {
        "name": "memory_record_evidence",
        "description": (
            "Record first-class evidence and optionally link it to the current milestone. "
            "This tool never executes commands. Material this project did not produce itself "
            "- a web page, someone else's issue or PR, a third-party document, anything quoted "
            "from outside the repository and its own tools - must be recorded with kind "
            "\"external\" and a provider naming where it came from. External evidence is kept "
            "and searchable, but it cannot support Truth and cannot carry a non-user Decision "
            "or Constraint into force. Mislabeling it as file/tool/user_instruction defeats that "
            "boundary and is a defect. The runtime assigns provenance and trust_level; callers "
            "cannot supply or raise either classification."
        ),
        "inputSchema": _schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["user_instruction", "file", "git", "test", "build", "tool", "screenshot", "artifact", "environment_probe", "external"],
                    "description": (
                        "Use \"external\" for anything this project did not produce itself - a web "
                        "page, someone else's issue or PR, a third-party document, any text quoted "
                        "from outside this repository and its own tools - and give a provider. "
                        "External evidence is kept and searchable, but it cannot support Truth and "
                        "cannot carry a non-user Decision or Constraint into force. Labeling it "
                        "file/tool/user_instruction instead defeats that boundary."
                    ),
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 16000},
                "milestone_id": {"type": "string", "maxLength": 128},
                "role": {"type": "string", "maxLength": 64},
                "path": {"type": "string", "maxLength": 4096},
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
                "command": {"type": "string", "maxLength": 16000},
                "result": {"type": "string", "maxLength": 16000},
                "exit_code": {"type": "integer"},
                "tool_name": {"type": "string", "maxLength": 256},
                "artifact_path": {"type": "string", "maxLength": 4096},
                "user_instruction": {"type": "string", "maxLength": 16000},
                "environment_probe": {"type": "string", "maxLength": 16000},
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "provider": {"type": "string", "maxLength": 128, "description": "Where the material came from. Required when kind is \"external\"."},
                "provider_thread_id": {"type": "string", "maxLength": 256},
            },
            ["kind", "summary", "created_by"],
        ),
    },
    {
        "name": "memory_record_verified_fact",
        "description": "Create Truth only from existing validated evidence. NO EVIDENCE -> NO TRUTH.",
        "inputSchema": _schema(
            {
                "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
                "evidence_ids": {"type": "array", "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"}, "minItems": 1, "uniqueItems": True},
                "verification_method": {"type": "string", "minLength": 1, "maxLength": 512},
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "scope": {"type": "string", "maxLength": 256},
                "contradicts": {"type": "array", "items": {"type": "string", "pattern": "^FACT-[0-9]{3,}$"}, "uniqueItems": True},
                "provider": {"type": "string", "maxLength": 128},
                "provider_thread_id": {"type": "string", "maxLength": 256},
            },
            ["statement", "evidence_ids", "verification_method", "created_by"],
        ),
    },
    {
        "name": "memory_store_department_rubric",
        "description": (
            "Propose the next version of a department acceptance rubric (R30). Version 1 "
            "is the runtime's. Only the department's lead or the on-call may propose, from "
            "its own thread; a version advances exactly once and cites outcome evidence - "
            "evidence an acceptance of this department rested on, as the runtime recorded it "
            "from another lead's thread, and none you wrote yourself."
        ),
        "inputSchema": _schema(
            {
                "department_id": {
                    "type": "string",
                    "pattern": "^[A-Za-z][A-Za-z0-9._-]{0,63}$",
                },
                "version": {"type": "integer", "minimum": 1},
                "criteria": {
                    "type": "array",
                    "minItems": 1,
                    "items": _schema(
                        {
                            "id": {
                                "type": "string",
                                "pattern": "^[A-Za-z][A-Za-z0-9._-]{0,63}$",
                            },
                            "requirement": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                        },
                        ["id", "requirement"],
                    ),
                },
                "standards": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"},
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            ["department_id", "version", "criteria", "evidence_ids", "created_by"],
        ),
    },
    {
        "name": "memory_record_verification_result",
        "description": (
            "Record an evidence-linked verification outcome with task/thread/turn "
            "provenance. A verification result is not Truth."
        ),
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "check_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "policy": {
                    "type": "string",
                    "enum": ["self", "deterministic", "independent", "auto"],
                },
                "verdict": {"type": "string", "enum": ["PASS", "REVISE"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 16000},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"},
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "provider": {"type": "string", "maxLength": 128},
                "provider_thread_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "provider_turn_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "details": {"type": "object"},
            },
            [
                "task_id",
                "check_id",
                "policy",
                "verdict",
                "summary",
                "evidence_ids",
                "created_by",
                "provider_thread_id",
                "provider_turn_id",
            ],
        ),
    },
    {
        "name": "memory_list_verification_results",
        "description": "List bounded verification outcomes for one task with cursor pagination.",
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
                "cursor": {"type": "string", "maxLength": 512},
            },
            ["task_id"],
        ),
    },
    {
        "name": "memory_add_observation",
        "description": "Record an agent hypothesis as permanently unverified Observation, never Truth.",
        "inputSchema": _schema(
            {
                "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "scope": {"type": "string", "maxLength": 256},
                "reason": {"type": "string", "maxLength": 16000},
                "provider": {"type": "string", "maxLength": 128},
                "provider_thread_id": {"type": "string", "maxLength": 256},
            },
            ["statement", "created_by"],
        ),
    },
    {
        "name": "memory_propose_decision",
        "description": "Create a Decision with explicit origin and lifecycle status. Decisions are not Truth.",
        "inputSchema": _schema(
            {
                "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
                "origin": {"type": "string", "enum": ["user", "agent", "project", "environment"]},
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "status": {"type": "string", "enum": ["proposed", "accepted"]},
                "reason": {"type": "string", "maxLength": 16000},
                "scope": {"type": "string", "maxLength": 256},
                "evidence_ids": {"type": "array", "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"}, "uniqueItems": True},
                "provider": {"type": "string", "maxLength": 128},
                "provider_thread_id": {"type": "string", "maxLength": 256},
            },
            ["statement", "origin", "created_by"],
        ),
    },
    {
        "name": "memory_set_decision_status",
        "description": "Accept, supersede, or reject an existing Decision while preserving history in the audit log.",
        "inputSchema": _schema(
            {
                "decision_id": {"type": "string", "pattern": "^DEC-[0-9]{3,}$"},
                "status": {"type": "string", "enum": ["proposed", "accepted", "superseded", "rejected"]},
                "actor": {"type": "string", "minLength": 1, "maxLength": 256},
                "reason": {"type": "string", "maxLength": 16000},
            },
            ["decision_id", "status", "actor"],
        ),
    },
    {
        "name": "memory_add_constraint",
        "description": "Record an active project constraint with explicit origin. A user constraint is still distinct from actual-state Truth.",
        "inputSchema": _schema(
            {
                "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
                "origin": {"type": "string", "enum": ["user", "agent", "project", "environment"]},
                "created_by": {"type": "string", "minLength": 1, "maxLength": 256},
                "scope": {"type": "string", "maxLength": 256},
                "reason": {"type": "string", "maxLength": 16000},
                "evidence_ids": {"type": "array", "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"}, "uniqueItems": True},
            },
            ["statement", "origin", "created_by"],
        ),
    },
    {
        "name": "memory_question",
        "description": "Open or resolve a project unknown. Resolution does not automatically create Truth.",
        "inputSchema": _schema(
            {
                "action": {"type": "string", "enum": ["open", "resolve"]},
                "question": {"type": "string", "maxLength": 8000},
                "question_id": {"type": "string", "pattern": "^Q-[0-9]{3,}$"},
                "created_by": {"type": "string", "maxLength": 256},
                "actor": {"type": "string", "maxLength": 256},
                "needed_for": {"type": "string", "maxLength": 256},
                "scope": {"type": "string", "maxLength": 256},
                "reason": {"type": "string", "maxLength": 16000},
                "evidence_ids": {"type": "array", "items": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"}, "uniqueItems": True},
            },
            ["action"],
        ),
    },
    {
        "name": "memory_attach_evidence",
        "description": "Attach supporting or contradictory evidence. Contradicting Truth opens a Conflict instead of overwriting it.",
        "inputSchema": _schema(
            {
                "record_id": {"type": "string", "pattern": "^[A-Z]+-[0-9]{3,}$"},
                "evidence_id": {"type": "string", "pattern": "^EVID-[0-9]{3,}$"},
                "relation": {"type": "string", "enum": ["supports", "contradicts"]},
                "actor": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            ["record_id", "evidence_id", "relation", "actor"],
        ),
    },
    {
        "name": "memory_conflict",
        "description": "Get or resolve a Conflict. Resolution history is retained.",
        "inputSchema": _schema(
            {
                "action": {"type": "string", "enum": ["get", "resolve"]},
                "conflict_id": {"type": "string", "pattern": "^CONFLICT-[0-9]{3,}$"},
                "outcome": {"type": "string", "enum": ["supersede_existing", "reject_incoming", "reverified_existing"]},
                "resolution": {"type": "string", "maxLength": 16000},
                "actor": {"type": "string", "maxLength": 256},
            },
            ["action", "conflict_id"],
        ),
    },
    {
        "name": "memory_user_correction",
        "description": "Record user desired state, supersede related agent Decisions, and open conflicts with still-verified actual state.",
        "inputSchema": _schema(
            {
                "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
                "related_ids": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]+-[0-9]{3,}$"}, "uniqueItems": True},
                "actor": {"type": "string", "maxLength": 256},
            },
            ["statement", "related_ids"],
        ),
    },
    {
        "name": "memory_milestone_evidence",
        "description": "Get bounded evidence recorded for a milestone.",
        "inputSchema": _schema(
            {
                "milestone_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["milestone_id"],
        ),
    },
]

ACTION_NAMES = tuple(item["name"].removeprefix("memory_") for item in _ACTION_DEFINITIONS)
_ACTION_BY_NAME = dict(zip(ACTION_NAMES, _ACTION_DEFINITIONS, strict=True))


def _combined_input_schema() -> dict[str, Any]:
    choices: list[dict[str, Any]] = []
    for action, definition in _ACTION_BY_NAME.items():
        source = definition["inputSchema"]
        choices.append(
            {
                "type": "object",
                # The operation description reaches the model only from here:
                # one tool is declared outside, and the signatures of
                # individual operations never made it in. What was written in
                # _ACTION_DEFINITIONS was dead text.
                "description": definition.get("description", ""),
                "additionalProperties": False,
                "properties": {
                    "operation": {"type": "string", "const": action},
                    **source.get("properties", {}),
                },
                "required": ["operation", *source.get("required", [])],
            }
        )
    return {"oneOf": choices}


# A single public tool means the user can grant Codex's built-in persistent
# trust once for this small project-memory surface. Some allowlisted operations
# write audited local records, so the conservative tool-level annotation must
# require approval even when the preflight itself calls the read-only branch.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory",
        "description": (
            "Access local evidence-backed Codex Autopilot Project Memory. "
            "Choose one allowlisted operation: " + ", ".join(ACTION_NAMES) + ". "
            "The tool is project-scoped, has no network or shell access, and preserves audit history."
        ),
        "inputSchema": _combined_input_schema(),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }
]


class MemoryMcpServer:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.memory = ProjectMemory(self.root)
        if (self.root / STATE_DIR_NAME / "config.toml").is_file():
            self.memory.initialize()
        self.actions: dict[str, Callable[[dict[str, Any]], Any]] = {
            "current": self._current,
            "search": self._search,
            "get": self._get,
            "record_evidence": self._record_evidence,
            "record_verified_fact": self._record_verified_fact,
            "store_department_rubric": self._store_department_rubric,
            "record_verification_result": self._record_verification_result,
            "list_verification_results": self._list_verification_results,
            "add_observation": self._add_observation,
            "propose_decision": self._propose_decision,
            "set_decision_status": self._set_decision_status,
            "add_constraint": self._add_constraint,
            "question": self._question,
            "attach_evidence": self._attach_evidence,
            "conflict": self._conflict,
            "user_correction": self._user_correction,
            "milestone_evidence": self._milestone_evidence,
        }

    @staticmethod
    def _validate_keys(args: Any, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise MemoryValidationError("tool arguments must be an object")
        unknown = set(args) - allowed
        if unknown:
            raise MemoryValidationError(
                f"unknown argument(s): {', '.join(sorted(unknown))}. "
                f"accepted here: {', '.join(sorted(allowed))}"
            )
        return args

    def _resolve_current_task(self, plan: Any, state: Any, requested: Any) -> Any:
        """A worker gets its own task, not the first one at hand.

        The old line `plan.milestones[state.milestone_index]` came from the
        sequential v0.7 model, where one pointer served the whole run. On a
        graph it stays zero: on the live 23-task v1.0 run with two parallel
        slots `milestone_index` was 0, and every worker received M1 in
        reply - someone else's task in the guise of its own. The preflight
        trust probe did not catch this hole: it runs before run-state is
        created and takes the `initialized: False` branch.
        """

        from .task_state import ACTIVE_TASK_STATES

        if requested is not None:
            if not isinstance(requested, str) or not requested:
                raise MemoryValidationError("task_id must be a non-empty string")
            task = plan.task_map.get(requested)
            if task is None:
                known = ", ".join(sorted(plan.task_map))
                raise MemoryValidationError(f"unknown task_id {requested!r}; plan has: {known}")
            return task
        if plan.legacy_serial:
            return plan.milestones[state.milestone_index]
        active = sorted(
            task_id
            for task_id, value in (state.task_states or {}).items()
            if value in {item.value for item in ACTIVE_TASK_STATES}
        )
        if not active:
            # Nothing to choose from means no guess either. The preflight
            # trust probe calls `current` exactly here: run-state has no
            # active task yet, and the probe has no task of its own at all.
            # A refusal at this point broke the launch out of nowhere.
            return None
        if len(active) == 1:
            return plan.task_map[active[0]]
        # Only real ambiguity is not resolved by guessing: several running
        # tasks. The old code silently answered about the milestone at index
        # zero, that is about M1.
        raise MemoryValidationError(
            "task_id is required on a task graph: this run has "
            f"{len(active)} active tasks ({', '.join(active)}) and the caller's task "
            "cannot be inferred. Pass the task_id from your own prompt."
        )

    def _current(self, args: dict[str, Any]) -> dict[str, Any]:
        """Hand over the task and the attested original user request.

        The request text is not embedded in the prompt as a copy - it is
        large and fixed for the whole run - so the worker fetches it here.
        The worker itself used to have to attest it: compare the length and
        sha256 from its prompt with the received text. That failed twice
        over.

        First, Codex's post-processing isolate has neither `crypto` nor
        `TextEncoder`: the worker has nothing to compute sha256 with. The
        contract allows exactly one call, which cannot be repeated. A trap:
        nothing to attest with, repeating forbidden - and the task honestly
        stopped with ENVIRONMENT_FAILURE. That is how M4 and M11 stopped.

        Second, a check done by the one being checked can be silently
        skipped, and nobody would notice.

        Now the server attests: the worker passes the expected hash from its
        prompt, the server hashes the text it is about to return, and either
        returns the attested text or fails closed. The worker need not
        compute or compare, and cannot skip the check.
        """

        args = self._validate_keys(args, {"task_id", "expect_user_request_sha256"})
        state_dir = self.root / STATE_DIR_NAME
        if not (state_dir / "config.toml").is_file():
            return {
                "project_root": str(self.root),
                "initialized": False,
                "truth_rule": "NO EVIDENCE -> NO TRUTH",
            }
        from .config import load_config
        from .plan import load_plan
        from .run_state import StateStore
        cfg = load_config(self.root)
        state = StateStore(state_dir).load()
        plan = load_plan(state_dir, cfg.profile)
        item = self._resolve_current_task(plan, state, args.get("task_id"))
        if item is None:
            query = " ".join([plan.goal, *(t.title for t in plan.tasks[:3])])
        else:
            query = " ".join([item.title, item.objective, *item.definition_of_done])
        request = plan.user_request
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        expected = args.get("expect_user_request_sha256")
        if expected is not None:
            if not isinstance(expected, str) or not expected.strip():
                raise MemoryValidationError(
                    "expect_user_request_sha256 must be a non-empty string"
                )
            if expected.strip().lower() != digest:
                raise MemoryValidationError(
                    "user_request does not match the digest this task was dispatched "
                    f"with: expected {expected.strip().lower()}, plan now has {digest}. "
                    "The text changed under the task - that is a reason to stop, not to "
                    "continue."
                )
        return {
            "project_root": str(self.root),
            "initialized": True,
            "goal": plan.goal,
            "user_request": request,
            "user_request_chars": len(request),
            "user_request_sha256": digest,
            "user_request_verified": expected is not None,
            "milestone": None if item is None else {
                "id": item.id,
                "title": item.title,
                "objective": item.objective,
                "definition_of_done": item.definition_of_done,
                "index": plan.milestones.index(item) + 1,
                "total": len(plan.milestones),
            },
            "critical_constraints": self.memory.critical_constraints(8),
            "relevant_memory": self.memory.context_ids(query, 8),
            "truth_rule": "NO EVIDENCE -> NO TRUTH",
        }

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"query", "categories", "scope", "limit", "cursor"})
        page = self.memory.search(query=args.get("query"), categories=args.get("categories"), scope=args.get("scope"), limit=args.get("limit", 8), cursor=args.get("cursor"))
        return {"records": page.records, "next_cursor": page.next_cursor, "limit": args.get("limit", 8)}

    def _get(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"id"})
        record_id = str(args.get("id") or "")
        if record_id.startswith("EVID-"):
            return self.memory.get_evidence(record_id)
        if record_id.startswith("VERIFY-"):
            return self.memory.get_verification_result(record_id)
        if record_id.startswith("CONFLICT-"):
            return self.memory.get_conflict(record_id)
        return self.memory.get_record(record_id)

    def _record_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"kind", "summary", "milestone_id", "role", "path", "line_start", "line_end", "command", "result", "exit_code", "tool_name", "artifact_path", "user_instruction", "environment_probe", "created_by", "provider", "provider_thread_id"}
        args = self._validate_keys(args, allowed)
        self._require_active_milestone_link(args)
        # R30: evidence under the runtime's tool names is the runtime's own
        # record (version 1 of a department rubric); from a model it would
        # pass for it.
        if str(args.get("tool_name") or "").strip().startswith(RESERVED_TOOL_PREFIX):
            raise MemoryValidationError(
                f"tool_name {args.get('tool_name')!r} is reserved for the Codex Autopilot runtime"
            )
        # Who wrote it, by the thread this server runs for - the audit keeps
        # it; the record's provider_thread_id is the model's to fill in.
        return self.memory.record_evidence(
            **args, writer_thread=str(os.environ.get("CODEX_THREAD_ID") or "") or None
        )

    def _require_active_milestone_link(self, args: dict[str, Any]) -> None:
        """While a milestone is in progress, evidence must name it.

        The completion gate asks for evidence linked to the milestone. A
        record without milestone_id used to be accepted silently, and the
        refusal came only after the whole turn was spent.

        Measured: worker M2 recorded four pieces of evidence, putting the
        milestone id into created_by ("M2-FILE-EXISTS") instead of
        milestone_id. Nothing landed in milestone_evidence, completion was
        refused, the turn was lost entirely.

        The milestone is not filled in for the worker: a link it did not
        name would be invented. The call is refused with the active
        milestone's name so it can repeat the call itself.

        A NAME is checked too, not only its presence. The gate used to
        refuse a missing milestone_id and accept any non-empty string -
        including the very label the measured worker put in created_by. Such
        a record lands under a milestone nobody will ask about: completion
        is then refused for "no new evidence" and the turn is lost exactly
        as it was before this gate existed, which is the one thing it was
        written to prevent.

        An id that names a real task of the plan is accepted even when that
        task is not the active one: the on-call engineer works outside
        active_task_ids by construction, and a gate that cannot pass is
        worse than no gate. What is refused is a name that belongs to no
        task at all.
        """

        named = str(args.get("milestone_id") or "").strip()
        active = self._active_task_ids()
        if not active:
            return
        if not named:
            raise MemoryValidationError(
                "milestone_id is required while a milestone is active: "
                + ", ".join(active)
                + ". Completion evidence must name its milestone; created_by is "
                "the author, not the milestone."
            )
        if named in active or named in self._plan_task_ids():
            return
        raise MemoryValidationError(
            f"milestone_id {named!r} names no task of this run. "
            "Evidence must name the milestone the completion gate will ask "
            "about, and the milestone in progress is: " + ", ".join(active)
        )

    def _plan_task_ids(self) -> frozenset[str]:
        """Every task id of the current plan, or nothing when it is unreadable.

        Unreadable means the active list is the only authority left, and the
        stricter answer is the right one there: a run always has a plan, so
        an unreadable one is not the ordinary case.
        """

        try:
            from .config import load_config
            from .plan import load_plan

            state_dir = self.root / STATE_DIR_NAME
            cfg = load_config(self.root)
            return frozenset(load_plan(state_dir, cfg.profile).task_map)
        except Exception:  # noqa: BLE001 - any failure to read leaves the strict path
            return frozenset()

    def _active_task_ids(self) -> tuple[str, ...]:
        state_path = self.root / STATE_DIR_NAME / "run-state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        active = payload.get("active_task_ids")
        if not isinstance(active, list):
            return ()
        return tuple(str(item) for item in active if str(item).strip())

    def _record_verified_fact(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"statement", "evidence_ids", "verification_method", "created_by", "scope", "contradicts", "provider", "provider_thread_id"}
        return self.memory.record_verified_fact(**self._validate_keys(args, allowed))

    def _store_department_rubric(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "department_id",
            "version",
            "criteria",
            "standards",
            "evidence_ids",
            "created_by",
        }
        args = self._validate_keys(args, allowed)
        # R30: only the department's lead or the on-call proposes a version,
        # known by its thread - never a worker of the tasks it judges. The
        # identity is CODEX_THREAD_ID, as for every ownership guard; a server
        # started without it refuses, and the proposal goes through
        # `codex-autopilot department-rubric-propose` from that thread.
        caller = str(os.environ.get("CODEX_THREAD_ID") or "")
        authorize_rubric_proposal(self.root, str(args.get("department_id") or ""), caller)
        reference = store_department_rubric(self.memory, **args, caller_thread_id=caller)
        return reference.to_dict()

    def _record_verification_result(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "task_id",
            "check_id",
            "policy",
            "verdict",
            "summary",
            "evidence_ids",
            "created_by",
            "provider",
            "provider_thread_id",
            "provider_turn_id",
            "details",
        }
        return self.memory.record_verification_result(
            **self._validate_keys(args, allowed)
        )

    def _list_verification_results(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"task_id", "limit", "cursor"})
        page = self.memory.list_verification_results(
            task_id=args.get("task_id"),
            limit=args.get("limit", 8),
            cursor=args.get("cursor"),
        )
        return {
            "results": page.records,
            "next_cursor": page.next_cursor,
            "limit": args.get("limit", 8),
        }

    def _add_observation(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"statement", "created_by", "confidence", "scope", "reason", "provider", "provider_thread_id"}
        return self.memory.add_observation(**self._validate_keys(args, allowed))

    @staticmethod
    def _refuse_owner_only(statement: Any, scope: Any = None) -> None:
        # R6: an agent's tool wrote an accepted "user" permission to delete
        # her project and the CLI executed it (the independent check's
        # probe). Her decisions on saved projects are not this tool's to
        # write, answer or revive (project_roots_change.owner_only_record).
        from .project_roots_change import owner_only_record

        reason = owner_only_record(statement, scope)
        if reason:
            raise MemoryValidationError(reason)

    def _propose_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"statement", "origin", "created_by", "status", "reason", "scope", "evidence_ids", "provider", "provider_thread_id"}
        args = self._validate_keys(args, allowed)
        self._refuse_owner_only(args.get("statement"), args.get("scope"))
        return self.memory.propose_decision(**args)

    def _set_decision_status(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"decision_id", "status", "actor", "reason"})
        record = self.memory.get_record(str(args.get("decision_id") or ""))
        self._refuse_owner_only(record.get("statement"), record.get("scope"))
        return self.memory.set_decision_status(args.get("decision_id"), args.get("status"), actor=args.get("actor"), reason=args.get("reason"))

    def _add_constraint(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.memory.add_constraint(**self._validate_keys(args, {"statement", "origin", "created_by", "scope", "reason", "evidence_ids"}))

    def _question(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"action", "question", "question_id", "created_by", "actor", "needed_for", "scope", "reason", "evidence_ids"})
        if args.get("action") == "open":
            return self.memory.open_question(question=args.get("question"), created_by=args.get("created_by"), needed_for=args.get("needed_for"), scope=args.get("scope", "project"))
        if args.get("action") == "resolve":
            return self.memory.resolve_question(args.get("question_id"), actor=args.get("actor"), reason=args.get("reason"), evidence_ids=args.get("evidence_ids", []))
        raise MemoryValidationError("action must be open or resolve")

    def _attach_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"record_id", "evidence_id", "relation", "actor"})
        return self.memory.attach_evidence(args.get("record_id"), args.get("evidence_id"), relation=args.get("relation"), actor=args.get("actor"))

    def _conflict(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"action", "conflict_id", "outcome", "resolution", "actor"})
        if args.get("action") == "get":
            return self.memory.get_conflict(args.get("conflict_id"))
        if args.get("action") == "resolve":
            return self.memory.resolve_conflict(args.get("conflict_id"), outcome=args.get("outcome"), resolution=args.get("resolution"), actor=args.get("actor"))
        raise MemoryValidationError("action must be get or resolve")

    def _user_correction(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"statement", "related_ids", "actor"})
        self._refuse_owner_only(args.get("statement"))
        return self.memory.apply_user_correction(statement=args.get("statement"), related_ids=args.get("related_ids", []), actor=args.get("actor", "user"))

    def _milestone_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        args = self._validate_keys(args, {"milestone_id", "limit"})
        return {"evidence": self.memory.milestone_evidence(args.get("milestone_id"), limit=args.get("limit", 20))}

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name != "memory":
            raise MemoryValidationError(f"unknown memory tool: {name}")
        if not isinstance(arguments, dict):
            raise MemoryValidationError("tool arguments must be an object")
        action = arguments.get("operation")
        handler = self.actions.get(action)
        if handler is None:
            raise MemoryValidationError(f"unknown memory action: {action!r}")
        return handler({key: value for key, value in arguments.items() if key != "operation"})

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:
            return None
        request_id = message["id"]
        method = message.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "codex-autopilot-project-memory",
                        "version": __version__,
                    },
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "tools/call":
                params = message.get("params") or {}
                data = self.call(str(params.get("name") or ""), params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}],
                    "structuredContent": data,
                    "isError": False,
                }
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (MemoryError, ValueError, TypeError, KeyError) as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}

    def serve(self) -> int:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("request must be an object")
                response = self.handle(message)
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Invalid request: {exc}"}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-autopilot memory-mcp")
    parser.add_argument("--project", type=Path)
    args = parser.parse_args(argv)
    root = args.project or resolve_canonical_project_root(Path(os.getcwd()))
    try:
        return MemoryMcpServer(root).serve()
    except (MemoryError, ValueError, OSError) as exc:
        print(f"Project Memory MCP failed to start: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
