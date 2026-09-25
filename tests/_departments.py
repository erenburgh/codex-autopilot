"""R30 fixtures: a plan in the shape of the live art run, and a run of it.

the art run (read-only, 24 Sep 2026): three roles - reference-artist,
character-artist, and art-reviewer "Character Art Verifier" with two
responsibilities and no verification_expectations - no `departments`, no
department or rubric bindings, and every task naming art-reviewer in
verification.verifier_role. M01 has no dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from _plan_contract import canonical_verification, canonicalize_plan


ART_REVIEWER = {
    "id": "art-reviewer",
    "name": "Character Art Verifier",
    "version": "1.0.0",
    "responsibilities": [
        "Независимо сопоставить результат с исходным запросом, оригиналом, картами и доказательствами.",
        "Не подменять художественное одобрение пользователя и не принимать непроверенные формы.",
    ],
}


def art_task(task_id: str, role: str, *, depends_on: tuple[str, ...] = ()) -> dict[str, Any]:
    verification = canonical_verification(verifier_role="art-reviewer")
    return {
        "id": task_id,
        "title": f"Model part {task_id}",
        "objective": f"Produce part {task_id} of the character.",
        "definition_of_done": [f"{task_id} matches the reference sheet."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files are sufficient for this fixture.",
        "reasoning": "medium",
        "role": role,
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": verification,
        "resources": [
            {"id": "asset-files", "kind": "directory", "target": f"Art/{task_id}", "access": "write"}
        ],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def art_run_plan(*, lead_on_every_task: bool = True) -> dict[str, Any]:
    tasks = [
        art_task("M01", "character-artist"),
        art_task("M02", "reference-artist", depends_on=("M01",)),
        art_task("M03", "character-artist", depends_on=("M02",)),
    ]
    raw = canonicalize_plan({
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Build the character from the reference sheet.",
        "user_request": "Сделай персонажа по референсу.",
        "model_strategy": "auto",
        "execution_strategy": "serial",
        "max_parallel_workers": 1,
        "computer_use_slots": 1,
        "roles": [
            {"id": "reference-artist", "name": "Reference Artist", "version": "1.0.0",
             "responsibilities": ["Prepare the reference sheets."]},
            {"id": "character-artist", "name": "Character Artist", "version": "1.0.0",
             "responsibilities": ["Model the character from the sheets."]},
            dict(ART_REVIEWER),
        ],
        "tasks": tasks,
    })
    if not lead_on_every_task:
        raw["tasks"][2]["verification"].pop("verifier_role")
    return raw


class DepartmentRun(unittest.TestCase):
    """A real run of a plan, driven through the production lifecycle."""

    plan_payload: Any = staticmethod(art_run_plan)

    def setUp(self) -> None:
        from _gates import patch_hook_trust_gates
        from codex_autopilot.config import load_config
        from codex_autopilot.memory import ProjectMemory
        from codex_autopilot.run_state import StateStore

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(self.plan_payload()), encoding="utf-8")
        self.initialize(plan_file, skill)
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.memory = ProjectMemory(self.root)

    def initialize(self, plan_file: Path, skill: Path) -> None:
        from _plan_contract import initialize_verified_project

        initialize_verified_project(
            self.root, plan_file, profile="adaptive", skill_path=skill,
            desktop_project_id="desktop-project",
        )

    def plan(self):
        from codex_autopilot.plan import load_plan

        return load_plan(self.cfg.state_dir, self.cfg.profile)

    def evidence(self, task_id: str, label: str, role: str = "implementation") -> None:
        self.memory.record_evidence(
            kind="test", summary=f"{label}.", created_by="department-test",
            milestone_id=task_id, role=role, command=f"check {label}", result="PASS", exit_code=0,
        )

    def mark_active(self, token: str, thread_id: str) -> None:
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item["reservation_token"] == token)
        session["thread_id"] = thread_id
        session["status"] = "ACTIVE"
        self.store.save(state)

    def reserve(self):
        from _relay import reserve_ready_frontier

        return reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner", hook_gate=lambda _cfg: None)

    def complete(self, thread_id: str, message: str, turn_id: str | None = None):
        from codex_autopilot.lifecycle_completion import complete_desktop_worker

        return complete_desktop_worker(
            self.cfg, thread_id=thread_id, turn_id=turn_id or f"turn-{thread_id}",
            final_message=message, hook_gate=lambda _cfg: None,
        )

    def implement(self, descriptor, thread_id: str):
        """Run one implementation to completion; returns the completion outcome."""

        from _handoff import bump_task_checkpoint

        self.mark_active(descriptor.reservation_token, thread_id)
        bump_task_checkpoint(self.root, descriptor.task_id, "Work done.")
        self.evidence(descriptor.task_id, f"implementation of {descriptor.task_id}")
        return self.complete(thread_id, "done\nAUTOPILOT_STATUS: ROTATE")

    def judge(self, descriptor, thread_id: str, message: str):
        from _handoff import bump_task_checkpoint

        self.mark_active(descriptor.reservation_token, thread_id)
        bump_task_checkpoint(self.root, descriptor.task_id, f"Judged by {thread_id}.")
        self.evidence(descriptor.task_id, f"lead check of {descriptor.task_id}", role="independent_verification")
        return self.complete(thread_id, message)

    def verdict(self, task_id: str, verdict: str = "PASS", issues=(), **fields: Any) -> str:
        from _plan_contract import attested_verdict

        if fields:
            payload = {"verdict": verdict, "issues": list(issues), **fields}
            return "AUTOPILOT_VERIFICATION: " + json.dumps(payload, separators=(",", ":"))
        return attested_verdict(self.cfg, task_id, verdict, issues)

    def rubric_records(self, department_id: str):
        from codex_autopilot.department_acceptance import rubric_scope

        return self.memory.list_records(
            categories=["truth"], statuses=["verified"], scope=rubric_scope(department_id), limit=20
        ).records
