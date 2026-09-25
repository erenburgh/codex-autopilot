"""R30: the department is derived from the worker's profession, never written.

What this replaces. R30 switched on only for a task that declared the
department-binding and rubric-binding resources; nobody wrote them, the live
art-run plan (18 tasks) had none, and the verifier fell back to
``verifier_role or task.role`` - the worker's own profession when the planner
left the field out. The rubric pin came from the evidence of a VERIFIED
dependency, so M01 (no dependencies) could not be bound at all. The tests of
that design (the dependency rubric tuple, the static-department override,
the bindings as the only door) are gone with it; these state the derived one.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from _departments import art_run_plan
from codex_autopilot.department_acceptance import (
    RUNTIME_RUBRIC_TOOL,
    DepartmentAcceptanceError,
    RubricReference,
    department_contract_from_raw,
    rubric_digest,
    rubric_scope,
    store_department_rubric,
    stored_rubric_versions,
    write_runtime_rubric,
)
from codex_autopilot.department_runtime import (
    derive_department_rubric,
    derive_task_department,
    ensure_department_rubric,
    load_task_department_acceptance,
    validate_department_leads,
)
from codex_autopilot.memory import MemoryValidationError, ProjectMemory
from codex_autopilot.plan import (
    plan_to_dict,
    validate_persisted_plan,
    validate_plan,
    validate_plan_change,
)
from codex_autopilot.plan_issues import PlanIssues
from codex_autopilot.verification import verifier_route

ROOT = Path(__file__).resolve().parents[1]
SAVED = json.loads((ROOT / "tests/fixtures/r30_saved_plans.json").read_text(encoding="utf-8"))


def _saved(name: str = "plain"):
    return validate_persisted_plan(json.loads(json.dumps(SAVED[name]["saved_plan"])), "adaptive")


class TheDepartmentIsTheWorkersProfessionTests(unittest.TestCase):
    def test_every_task_is_judged_by_its_professions_lead(self) -> None:
        """A task that names no lead itself is still judged by its profession's.

        Mutation: verification.verifier_route back to ``verifier_role or
        task.role`` - M03 goes to its own profession, character-artist.
        """

        raw = json.loads(json.dumps(SAVED["plain"]["saved_plan"]))
        raw["tasks"][2]["verification"].pop("verifier_role")
        plan = validate_persisted_plan(raw, "adaptive")
        self.assertEqual({verifier_route(plan, task).role_id for task in plan.tasks}, {"art-reviewer"})
        self.assertEqual(
            derive_task_department(plan, plan.task_map["M03"]),
            derive_task_department(plan, plan.task_map["M01"]),
        )
        department = derive_task_department(plan, plan.task_map["M01"])
        self.assertEqual((department.id, department.lead_role_id), ("art-reviewer", "art-reviewer"))

    def test_a_declared_department_names_it_and_keeps_its_rubric_verbatim(self) -> None:
        """id and name from the entry; the 0.13 `rubric` tuple kept byte for byte.

        Mutation: DepartmentContract.to_dict without `rubric` - the saved
        plan's digest moves and a running run's PLAN_VERIFIED breaks.
        """

        plan = _saved("declared")
        department = derive_task_department(plan, plan.task_map["M02"])
        self.assertEqual((department.id, department.name), ("character-art", "Character Art"))
        self.assertEqual(plan_to_dict(plan)["departments"], SAVED["declared"]["saved_plan"]["departments"])

    def test_a_binding_that_disagrees_with_the_lead_is_refused(self) -> None:
        raw = json.loads(json.dumps(SAVED["declared"]["saved_plan"]))
        raw["tasks"][0]["resources"] += [
            {"id": "department-binding", "kind": "logical", "target": "department-id:other", "access": "read"},
            {"id": "rubric-binding", "kind": "logical",
             "target": "project-memory:department-acceptance-rubric:other", "access": "read"},
        ]
        plan = validate_persisted_plan(raw, "adaptive")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "binds department 'other'"):
            derive_task_department(plan, plan.task_map["M01"])

    def test_a_department_entry_without_rubric_is_accepted_and_names_what_is(self) -> None:
        contract = department_contract_from_raw(
            {"id": "character-art", "name": "Character Art", "lead_role_id": "art-reviewer"}, "department 1"
        )
        self.assertIsNone(contract.rubric)
        self.assertNotIn("rubric", contract.to_dict())
        with self.assertRaises(DepartmentAcceptanceError) as caught:
            department_contract_from_raw({"id": "x", "name": "X", "lead_role": "y"}, "department 1")
        self.assertIn("accepted fields are", str(caught.exception))
        self.assertIn("lead_role_id", str(caught.exception))


class AdmissionNamesEveryLeadViolationTests(unittest.TestCase):
    def _raw(self):
        raw = art_run_plan()
        first, second, third = (json.loads(json.dumps(item)) for item in raw["tasks"])
        a = dict(first, id="A")
        a["verification"].pop("verifier_role")                          # A: no lead
        b = dict(second, id="B", depends_on=[])
        b["verification"]["verifier_role"] = "reference-artist"          # B: itself
        c = dict(third, id="C", depends_on=[])                            # C: art-reviewer
        d = json.loads(json.dumps(first))
        d.update(id="D")
        d["verification"]["verifier_role"] = "reference-artist"          # D: a second lead
        raw["tasks"] = [a, b, c, d]
        return raw

    def test_every_class_in_one_refusal(self) -> None:
        """Missing, itself, and one profession with two leads - all at once.

        Mutation: validate_department_leads returns after its first class -
        the refusal names A only and the replanner spends a round per class.
        """

        with self.assertRaises(PlanIssues) as caught:
            validate_plan(self._raw(), "adaptive")
        leads = [item for item in caught.exception.issues if item.stage == "leads"]
        text = " ".join(item.message for item in leads)
        self.assertEqual(len(leads), 3, text)
        self.assertIn("missing for: A", text)
        self.assertIn("B (reference-artist)", text)
        self.assertIn("role 'character-artist' name several", text)
        self.assertIn("art-reviewer: C", text)
        self.assertIn("reference-artist: D", text)

    def test_a_plan_change_is_refused_the_same_way_and_untouched_tasks_keep_what_they_had(self) -> None:
        """New and rewritten tasks need a lead; a task the change leaves alone does not.

        A VERIFIED contract is immutable, so requiring a lead of an untouched
        task would make every change of a pre-R30 plan impossible.
        Mutation: drop the unchanged-task exemption - the change, which
        leaves M03 (no lead of its own) untouched, is refused.
        """

        raw = json.loads(json.dumps(SAVED["plain"]["saved_plan"]))
        raw["tasks"][2]["verification"].pop("verifier_role")
        current = validate_persisted_plan(raw, "adaptive")
        change = plan_to_dict(current)
        change["graph_version"] = 2
        added = json.loads(json.dumps(change["tasks"][0]))
        added.update(id="M04", depends_on=["M03"])
        added["verification"].pop("verifier_role")
        change["tasks"].append(added)
        with self.assertRaisesRegex(PlanIssues, "missing for: M04"):
            validate_plan_change(current, change, "adaptive")
        added["verification"]["verifier_role"] = "art-reviewer"
        self.assertIn("M04", validate_plan_change(current, change, "adaptive").task_map)

    def test_a_saved_plan_with_no_lead_at_all_still_loads(self) -> None:
        """A running run is stopped per task when a lead is needed, never on load.

        Mutation: require_leads on in validate_persisted_plan - this raises,
        and status, reservation and completion all go down with it.
        """

        raw = json.loads(json.dumps(SAVED["plain"]["saved_plan"]))
        for item in raw["tasks"]:
            item["verification"].pop("verifier_role")
        plan = validate_persisted_plan(raw, "adaptive")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "no Lead Role is defined for role"):
            derive_task_department(plan, plan.task_map["M01"])

    def test_the_skill_examples_name_one_lead_per_profession(self) -> None:
        for path in (ROOT / "plugins").rglob("SKILL.md"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(skill=path.parent.name):
                self.assertIn("## Departments (R30)", text)
                self.assertIn("codex-autopilot department-rubric-propose", text)
                example = next(
                    json.loads(line) for line in text.splitlines() if line.startswith('{"schema_version":3')
                )
                roles = [types.SimpleNamespace(id=item["id"], name=item["name"]) for item in example["roles"]]
                tasks = [
                    types.SimpleNamespace(
                        id=item["id"], role=item["role"], resources=(),
                        verification=types.SimpleNamespace(verifier_role=item["verification"].get("verifier_role")),
                    )
                    for item in example["tasks"]
                ]
                self.assertEqual(validate_department_leads(tasks, roles), [])
                lead_id = example["tasks"][0]["verification"]["verifier_role"]
                lead = next(item for item in example["roles"] if item["id"] == lead_id)
                self.assertTrue(lead.get("verification_expectations"))


class TheSavedPlanDigestDoesNotMoveTests(unittest.TestCase):
    def test_art_run_shape_keeps_the_digest_its_receipt_was_bound_to(self) -> None:
        """The pre-R30 runtime's digest of the same saved plan, recorded in the fixture.

        Measured on the live run too (read-only): the digest did not move
        before and after. Mutation: plan_to_dict writes the derived departments - the
        digest moves and PLAN_VERIFIED no longer holds.
        """

        from _plan_contract import canonical_plan_verification
        from codex_autopilot.plan_verification import plan_sha256, require_plan_verified

        for name in ("plain", "declared"):
            with self.subTest(plan=name):
                plan = _saved(name)
                self.assertEqual(plan_sha256(plan), SAVED[name]["plan_sha256"])
                receipt = canonical_plan_verification(plan)
                self.assertEqual(receipt["plan_sha256"], SAVED[name]["plan_sha256"])
                require_plan_verified(plan, receipt)


class _Memory(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        self.memory = ProjectMemory(self.root)
        self.plan = _saved()
        self.department = derive_task_department(self.plan, self.plan.task_map["M01"])


class TheRuntimeWritesVersionOneTests(_Memory):
    def test_version_one_is_derived_from_the_leads_profile(self) -> None:
        reference, drift = ensure_department_rubric(self.memory, self.plan, self.department)
        self.assertFalse(drift)
        history = stored_rubric_versions(self.memory, "art-reviewer")
        self.assertEqual([item.reference for item in history], [reference])
        rubric = history[0].rubric
        self.assertEqual(reference.sha256, rubric_digest(rubric))
        self.assertEqual(
            [item.id for item in rubric.criteria],
            ["request-fidelity", "dod-coverage", "independent-evidence", "lead-expectation-1", "lead-expectation-2"],
        )
        record = self.memory.get_record(reference.record_id)
        self.assertEqual(record["scope"], rubric_scope("art-reviewer"))
        self.assertEqual([item["tool_name"] for item in record["evidence"]], [RUNTIME_RUBRIC_TOOL])
        # Project Memory outlives the run: no criterion carries this run's request.
        self.assertNotIn("Сделай персонажа", json.dumps(rubric.to_dict(), ensure_ascii=False))

    def test_a_second_write_returns_the_first(self) -> None:
        """Mutation: write_runtime_rubric without its existing-history check - two v1."""

        rubric = derive_department_rubric(self.plan, self.department)
        first = write_runtime_rubric(self.memory, rubric, evidence_result={"department_id": "art-reviewer"})
        second = write_runtime_rubric(self.memory, rubric, evidence_result={"department_id": "art-reviewer"})
        self.assertEqual(first, second)
        again, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        self.assertEqual(again, first)
        self.assertEqual(len(stored_rubric_versions(self.memory, "art-reviewer")), 1)

    def test_writers_racing_for_version_one_write_it_once(self) -> None:
        """Check and write are one step for every writer (the rubric lock).

        The read is slowed so both writers would see an empty history.
        Mutation: _rubric_write_lock a no-op - two v1, and the history is
        ambiguous for good.
        """

        import codex_autopilot.department_acceptance as module

        real = module.stored_rubric_versions

        def slow(memory, department_id):
            found = real(memory, department_id)
            time.sleep(0.2)
            return found

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        rubric = derive_department_rubric(self.plan, self.department)

        def write() -> None:
            try:
                barrier.wait()
                write_runtime_rubric(ProjectMemory(self.root), rubric, evidence_result={"department_id": "art-reviewer"})
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        with mock.patch.object(module, "stored_rubric_versions", slow):
            workers = [threading.Thread(target=write) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(errors, [])
        records = self.memory.list_records(
            categories=["truth"], statuses=["verified"], scope=rubric_scope("art-reviewer"), limit=20
        ).records
        self.assertEqual(len(records), 1)

    def test_a_changed_lead_profile_does_not_rewrite_the_rubric(self) -> None:
        """The defect is recorded once and shown to the lead; v1 stays current.

        Mutation: ensure_department_rubric writes the next version from the
        changed profile - the history grows to two without outcome evidence.
        """

        reference, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        lead = self.plan.role_map["art-reviewer"]
        changed = replace(self.plan, roles=tuple(
            replace(item, verification_expectations=("Everything is perfect.",)) if item.id == lead.id else item
            for item in self.plan.roles
        ))
        for _ in range(2):
            again, drift = ensure_department_rubric(self.memory, changed, self.department)
            self.assertEqual(again, reference)
            self.assertTrue(drift)
        self.assertEqual(len(stored_rubric_versions(self.memory, "art-reviewer")), 1)
        observations = self.memory.list_records(
            categories=["observation"], scope="department-acceptance-drift:art-reviewer", limit=20
        ).records
        self.assertEqual(len(observations), 1)
        loaded = load_task_department_acceptance(self.memory, changed, changed.task_map["M01"], ensure=False)
        self.assertTrue(loaded.to_dict()["lead_profile_changed_since_v1"])


class OnlyOutcomesChangeTheRubricTests(_Memory):
    def setUp(self) -> None:
        super().setUp()
        self.v1, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        self.v2 = dict(
            department_id="art-reviewer", version=2,
            criteria=[{"id": "silhouette", "requirement": "The silhouette matches the sheet."}],
            standards=[], created_by="Character Art Verifier",
        )

    def _evidence(self, **fields):
        base = dict(kind="test", summary="Outcome.", command="compare", result="PASS", exit_code=0,
                    created_by="department-test")
        base.update(fields)
        return str(self.memory.record_evidence(**base)["id"])

    def test_version_one_is_never_a_models(self) -> None:
        with self.assertRaisesRegex(DepartmentAcceptanceError, "immutable"):
            store_department_rubric(self.memory, **dict(self.v2, version=1), evidence_ids=[self._evidence()])

    def _acceptance(self, evidence_id: str, thread: str = "lead-1") -> None:
        """What completion records for a lead's verdict (lifecycle_completion)."""

        self.memory._record_runtime_verification_result(
            task_id="M01", check_id="independent-acceptance", policy="independent", verdict="REVISE",
            summary="Lead asked for revision.", evidence_ids=[evidence_id], created_by="Character Art Verifier",
            provider="codex-desktop", provider_thread_id=thread, provider_turn_id=f"turn-{thread}",
            details={"department_acceptance": {"department": {"id": "art-reviewer"}}},
        )

    def test_a_new_version_needs_an_acceptance_of_the_department(self) -> None:
        """An observation, a stray record or the runtime's own is not an outcome.

        Mutation: _require_outcome_evidence back to "any outcome kind" - the
        plain test record is taken and v2 is written from nothing.
        """

        stray = self._evidence()
        with self.assertRaisesRegex(DepartmentAcceptanceError, "recorded it"):
            store_department_rubric(self.memory, **self.v2, evidence_ids=[stray])
        runtime = self.memory.get_record(self.v1.record_id)["evidence"][0]["id"]
        with self.assertRaisesRegex(DepartmentAcceptanceError, "runtime's own record"):
            store_department_rubric(self.memory, **self.v2, evidence_ids=[runtime])
        judged = self._evidence()
        self._acceptance(judged)
        reference = store_department_rubric(self.memory, **self.v2, evidence_ids=[judged])
        self.assertEqual(reference.version, 2)
        self.assertEqual(
            [item.reference.version for item in stored_rubric_versions(self.memory, "art-reviewer")], [1, 2]
        )

    def test_an_outcome_is_the_runtimes_record_not_the_models(self) -> None:
        """The independent check forged an outcome in two MCP calls; now it is refused.

        Evidence with provider_thread_id "someone-else", and a verification
        result whose details name the department. Written here from another
        model's thread, so the writer check (below) does not catch it first:
        the result itself is what is refused. Mutation: _runtime_acceptance_of
        without the runtime attestation - a model's own result is the outcome.
        """

        from codex_autopilot.memory_mcp import MemoryMcpServer

        server = MemoryMcpServer(self.root)
        with mock.patch.dict("os.environ", {"CODEX_THREAD_ID": "worker-M02"}):
            evidence = server.actions["record_evidence"]({
                "kind": "test", "summary": "s", "command": "true", "result": "ok", "exit_code": 0,
                "created_by": "lead", "provider_thread_id": "someone-else",
            })["id"]
            server.actions["record_verification_result"]({
                "task_id": "M01", "check_id": "x", "policy": "independent", "verdict": "REVISE",
                "summary": "s", "evidence_ids": [evidence], "created_by": "lead",
                "provider_thread_id": "someone-else", "provider_turn_id": "t",
                "details": {"department_acceptance": {"department": {"id": "art-reviewer"}}},
            })
        with self.assertRaisesRegex(DepartmentAcceptanceError, "as the runtime recorded it"):
            store_department_rubric(self.memory, **self.v2, evidence_ids=[evidence], caller_thread_id="lead-thread")
        self.assertEqual(len(stored_rubric_versions(self.memory, "art-reviewer")), 1)

    def test_the_proposers_own_thread_is_read_from_the_journal(self) -> None:
        """Written by the proposer, or judged by it: not an outcome, whatever the fields say.

        The evidence names another thread in provider_thread_id; Project
        Memory's audit knows the server that wrote it ran for the proposer.
        Mutations: _require_outcome_evidence without its writer-thread check;
        the MCP evidence door without its writer_thread stamp; an acceptance
        judged in the proposer's own thread counted as an outcome.
        """

        from codex_autopilot.memory_mcp import MemoryMcpServer

        with mock.patch.dict("os.environ", {"CODEX_THREAD_ID": "lead-thread"}):
            mine = MemoryMcpServer(self.root).actions["record_evidence"]({
                "kind": "test", "summary": "s", "command": "true", "result": "ok", "exit_code": 0,
                "created_by": "lead", "provider_thread_id": "someone-else",
            })["id"]
        self.assertEqual(self.memory.evidence_writer_thread(mine), "lead-thread")
        self._acceptance(mine, thread="lead-2")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "proposing thread itself"):
            store_department_rubric(self.memory, **self.v2, evidence_ids=[mine], caller_thread_id="lead-thread")
        judged_by_me = self._evidence()
        self._acceptance(judged_by_me, thread="lead-thread")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "another lead's thread"):
            store_department_rubric(
                self.memory, **self.v2, evidence_ids=[judged_by_me], caller_thread_id="lead-thread"
            )
        self.assertEqual(len(stored_rubric_versions(self.memory, "art-reviewer")), 1)
        reference = store_department_rubric(
            self.memory, **self.v2, evidence_ids=[judged_by_me], caller_thread_id="on-call"
        )
        self.assertEqual(reference.version, 2)


class TheScopeIsClosedToModelsTests(_Memory):
    def test_no_model_door_writes_into_a_rubric_scope(self) -> None:
        """Mutation: drop the reserved-scope check in memory._create_record."""

        from codex_autopilot.memory_mcp import MemoryMcpServer

        evidence = str(self.memory.record_evidence(
            kind="test", summary="x", command="x", result="PASS", exit_code=0, created_by="t")["id"])
        server = MemoryMcpServer(self.root)
        with self.assertRaisesRegex(MemoryValidationError, "reserved for department rubrics"):
            server.actions["record_verified_fact"]({
                "statement": "{}", "evidence_ids": [evidence], "verification_method": "m",
                "created_by": "worker", "scope": rubric_scope("art-reviewer"),
            })
        with self.assertRaisesRegex(MemoryValidationError, "reserved for department rubrics"):
            server.actions["add_observation"]({
                "statement": "the rubric is empty now", "created_by": "worker",
                "scope": rubric_scope("art-reviewer"),
            })
        with self.assertRaisesRegex(MemoryValidationError, "reserved for the Codex Autopilot runtime"):
            server.actions["record_evidence"]({
                "kind": "tool", "summary": "fake runtime record", "tool_name": RUNTIME_RUBRIC_TOOL,
                "created_by": "worker",
            })

    def test_no_model_door_changes_a_rubrics_status(self) -> None:
        """Measured by the independent check: `contradicts` made v1 disputed, the
        history read empty, the runtime wrote a second v1, and resolving the
        conflict made both verified - "ambiguous" for good.

        Every door is tried the way a model calls it (MCP), and nothing may
        be half-written by a refused call. Mutations, each alone: the check
        before a contradicting fact is written (the fact is left behind); the
        one in attach_evidence; the one before a user correction's decision;
        the one in open_conflict_in_transaction; the one in resolve_conflict.
        """

        from codex_autopilot.memory_mcp import MemoryMcpServer

        v1, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        server = MemoryMcpServer(self.root)
        evidence = str(server.actions["record_evidence"]({
            "kind": "test", "summary": "x", "command": "x", "result": "PASS", "exit_code": 0,
            "created_by": "worker"})["id"])
        facts_before = self.memory.list_records(categories=["truth"], limit=20).records
        with self.assertRaisesRegex(MemoryValidationError, "R30: .*department rubric"):
            server.actions["record_verified_fact"]({
                "statement": "the rubric is wrong", "evidence_ids": [evidence], "verification_method": "m",
                "created_by": "worker", "scope": "notes", "contradicts": [v1.record_id],
            })
        self.assertEqual(self.memory.list_records(categories=["truth"], limit=20).records, facts_before)
        for relation in ("contradicts", "supports"):
            with self.assertRaisesRegex(MemoryValidationError, "R30: .*department rubric"):
                server.actions["attach_evidence"]({
                    "record_id": v1.record_id, "evidence_id": evidence, "relation": relation, "actor": "worker",
                })
        with self.assertRaisesRegex(MemoryValidationError, "R30: .*department rubric"):
            server.actions["user_correction"]({
                "statement": "Use my rubric instead.", "related_ids": [v1.record_id], "actor": "user",
            })
        self.assertEqual(self.memory.list_records(categories=["decision"], limit=20).records, [])
        with self.assertRaisesRegex(MemoryValidationError, "R30: .*department rubric"):
            self.memory.open_conflict(existing_record_id=v1.record_id, statement="x", created_by="worker")
        self.assertEqual(self.memory.get_record(v1.record_id)["status"], "verified")
        self.assertEqual(self.memory.get_record(v1.record_id)["evidence"][0]["tool_name"], RUNTIME_RUBRIC_TOOL)
        self.assertEqual([item.reference for item in stored_rubric_versions(self.memory, "art-reviewer")], [v1])

    def test_a_dispute_left_by_an_older_build_never_returns_a_rubric_to_verified(self) -> None:
        """The conflict an older build opened stays resolvable only by retiring the record.

        Mutation: resolve_conflict without its rubric check - reject_incoming
        returns the disputed v1 to verified beside the v1 written since:
        ambiguous again.
        """

        from codex_autopilot.memory_mcp import MemoryMcpServer

        v1, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        with self.memory._connect(write=True) as db:  # what the 0.14 build let any model do
            db.execute("UPDATE records SET status='disputed' WHERE id=?", (v1.record_id,))
            db.execute(
                "INSERT INTO conflicts(id,existing_record_id,statement,status,created_by,created_at) "
                "VALUES('CONFLICT-900',?,'legacy','needs_review','worker','2026-09-01T00:00:00+00:00')",
                (v1.record_id,),
            )
        again, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        self.assertNotEqual(again.record_id, v1.record_id)
        server = MemoryMcpServer(self.root)
        for outcome in ("reject_incoming", "reverified_existing"):
            with self.assertRaisesRegex(MemoryValidationError, "R30: .*department rubric"):
                server.actions["conflict"]({
                    "action": "resolve", "conflict_id": "CONFLICT-900", "outcome": outcome,
                    "resolution": "r", "actor": "worker",
                })
        server.actions["conflict"]({
            "action": "resolve", "conflict_id": "CONFLICT-900", "outcome": "supersede_existing",
            "resolution": "retired", "actor": "worker",
        })
        self.assertEqual(self.memory.get_record(v1.record_id)["status"], "superseded")
        self.assertEqual([item.reference for item in stored_rubric_versions(self.memory, "art-reviewer")], [again])

    def test_the_mcp_rubric_door_needs_a_known_proposer(self) -> None:
        """A server with no caller identity refuses; it never writes on trust.

        Mutation: remove authorize_rubric_proposal from the MCP door.
        """

        from codex_autopilot.memory_mcp import MemoryMcpServer

        ensure_department_rubric(self.memory, self.plan, self.department)
        judged = str(self.memory.record_evidence(
            kind="test", summary="x", command="x", result="PASS", exit_code=0, created_by="t")["id"])
        self.memory._record_runtime_verification_result(
            task_id="M01", check_id="independent-acceptance", policy="independent", verdict="PASS",
            summary="accepted", evidence_ids=[judged], created_by="Character Art Verifier",
            provider="codex-desktop", provider_thread_id="lead-1", provider_turn_id="turn-1",
            details={"department_acceptance": {"department": {"id": "art-reviewer"}}},
        )
        server = MemoryMcpServer(self.root)
        with mock.patch.dict("os.environ", {"CODEX_THREAD_ID": ""}):
            with self.assertRaisesRegex(DepartmentAcceptanceError, "own thread"):
                server.actions["store_department_rubric"]({
                    "department_id": "art-reviewer", "version": 2,
                    "criteria": [{"id": "x", "requirement": "y"}], "standards": [],
                    "evidence_ids": [judged], "created_by": "worker",
                })
        self.assertEqual(len(stored_rubric_versions(self.memory, "art-reviewer")), 1)


class AnAmbiguousHistoryIsNamedTests(_Memory):
    def test_a_stray_record_is_refused_with_its_id_until_superseded(self) -> None:
        """Mutation: stored_rubric_versions without its 1..n continuity check."""

        reference, _ = ensure_department_rubric(self.memory, self.plan, self.department)
        rubric = derive_department_rubric(self.plan, self.department)
        stray = self.memory.record_verified_fact(
            statement=json.dumps(rubric.to_dict(), sort_keys=True, separators=(",", ":")),
            evidence_ids=[self.memory.get_record(reference.record_id)["evidence"][0]["id"]],
            verification_method="legacy write", created_by="old-model", scope=rubric_scope("art-reviewer"),
            reserved_scope=True,  # what any model could do before the scope was reserved
        )
        with self.assertRaises(DepartmentAcceptanceError) as caught:
            stored_rubric_versions(self.memory, "art-reviewer")
        self.assertIn(str(stray["id"]), str(caught.exception))
        self.assertIn("supersede", str(caught.exception))
        self.memory._set_record_status(str(stray["id"]), "truth", "superseded", "on-call", "stray")
        self.assertEqual([item.reference for item in stored_rubric_versions(self.memory, "art-reviewer")], [reference])


class AttestationTests(unittest.TestCase):
    def test_what_counts_toward_the_limit_is_the_leads_own_mistake(self) -> None:
        """Mutation: attestation_refusal counts every refusal (True always)."""

        from codex_autopilot.department_runtime import attestation_refusal

        expected = RubricReference("FACT-002", 2, "b" * 64)
        given = {"descriptor": {"prompt": '"department_acceptance":{"reference":{"record_id":"FACT-002","version":2,"sha256":"' + "b" * 64 + '"}}'}}
        reason, counted = attestation_refusal(expected, None, given)
        self.assertTrue(counted)
        self.assertIn('"rubric":{"record_id":"FACT-002"', reason)
        self.assertIsNone(attestation_refusal(expected, expected, given))
        advanced = {"descriptor": {"prompt": '"department_acceptance" FACT-001 ' + "a" * 64}}
        reason, counted = attestation_refusal(expected, RubricReference("FACT-001", 1, "a" * 64), advanced)
        self.assertFalse(counted)
        self.assertIn("advanced to version 2", reason)
        reason, counted = attestation_refusal(expected, None, {"descriptor": {"prompt": "old runtime"}})
        self.assertFalse(counted)
        self.assertIn("launched without", reason)


if __name__ == "__main__":
    unittest.main()
