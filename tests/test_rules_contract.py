"""The rules as an executable contract.

The rules file by itself holds nothing. What is checked here is that the
declared enforcement mode matches reality, and that unimplemented rules
are visible rather than silent.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

from _plan_contract import canonicalize_plan
from codex_autopilot.plan import validate_migrating_plan, validate_plan

from codex_autopilot.rules import (
    CHECKED,
    ENFORCED,
    RULES,
    record_violation,
    rule,
    rules_for_prompt,
)


SRC = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"

# Rule -> the test that fails when it is violated.
# An entry here means: the check exists and is proven.
IMPLEMENTED = {
    "R2": "test_r2_codex_app_task_api_is_absent_from_production",
    "R8": "test_r8_self_acceptance_is_rejected_by_plan_validation",
    "R21": "tests/test_clean_environment.py",
    "R9": "test_workspace_ux.py::test_production_title_dispatch_rejects_a_missing_role",
    "R17": "test_r17_rules_come_before_specifications_and_are_not_truncatable",
    "R13": "test_r13_escalation_requires_a_reason_from_the_closed_list",
    "R6": "test_r6_preflight_rejects_a_target_outside_desktop_root_paths",
    "R1": "test_r1_owner_that_never_completed_is_reported",
    "R5": "test_r5_project_id_is_never_reported_as_sidebar_placement",
    "R7": "test_declared_scope.py::test_change_outside_the_declared_area_is_recorded_on_completion",
    "R16": "test_declared_scope.py::test_r16_report_without_applied_rules_is_recorded",
    "R18": "test_rule_contract_and_external_input.py::ExternalInputTests",
    "R32": "test_user_unblock.py::R32InterventionIsRecorded",
    "R31": "test_early_gate.py::EarlyMilestoneLinkTests",
    "R19": "test_no_unreachable_contracts.py::NoUnreachableContractTests",
    "R23": "test_retry_budget.py::RetryBudgetTests",
    "R29": "test_task_graph.py::test_implemented_is_not_verified_when_verification_is_required",
    "R28": "test_core.py::PurgeAndReplaceSnapshotTests",
}

# Rules whose check is not yet written. The list is deliberately explicit:
# an empty line here would mean everything is covered, which is untrue.
PENDING = {
    "R3", "R4", "R10", "R11", "R12",
    "R14", "R15", "R20", "R22",
    "R24", "R25", "R26", "R27", "R30",
}


class RuleRegistryTests(unittest.TestCase):
    def test_every_rule_declares_a_mode_and_a_check(self) -> None:
        for item in RULES:
            with self.subTest(rule=item.id):
                self.assertIn(item.mode, {ENFORCED, CHECKED})
                self.assertTrue(item.statement.strip(), "statement is empty")
                self.assertTrue(item.check.strip(), "check specification is empty")

    def test_rule_ids_are_unique_and_contiguous(self) -> None:
        ids = [item.id for item in RULES]
        self.assertEqual(len(ids), len(set(ids)))
        numbers = sorted(int(item[1:]) for item in ids)
        self.assertEqual(numbers, list(range(1, len(ids) + 1)))

    def test_implemented_and_pending_together_cover_every_rule(self) -> None:
        covered = set(IMPLEMENTED) | PENDING
        self.assertEqual(
            covered,
            {item.id for item in RULES},
            "every rule is either implemented or explicitly listed as "
            "unimplemented",
        )
        self.assertFalse(
            set(IMPLEMENTED) & PENDING,
            "a rule cannot be both implemented and pending",
        )

    def test_every_implemented_pointer_names_a_test_that_exists(self) -> None:
        """A pointer to a check is a promise; a dangling one voids it.

        The 15.09 audit (D4) found the map wrong in both directions:
        R19 and R29 were listed as pending while their checks were
        ready, and the R9 pointer led to "thread_titles._role_segment
        + test_workspace_ux" - neither that file nor a test by that
        name exists. Measured 17.09: 13 of 14 pointers resolved, one
        did not. From now on every one must resolve:
        ``file.py::name`` - to a def or class inside that file,
        ``tests/file.py`` - to a file, a bare ``test_...`` - to a def
        in any test file.
        """

        import re

        tests_dir = Path(__file__).resolve().parent
        sources = {path.name: path.read_text(encoding="utf-8") for path in tests_dir.glob("test_*.py")}
        dangling: list[str] = []
        for rule_id, pointer in IMPLEMENTED.items():
            if "::" in pointer:
                file_name, _, name = pointer.partition("::")
                text = sources.get(file_name)
                found = text is not None and re.search(rf"^\s*(def|class) {re.escape(name)}\b", text, re.M)
            elif pointer.startswith("tests/"):
                found = pointer.removeprefix("tests/") in sources
            else:
                found = any(re.search(rf"^\s*def {re.escape(pointer)}\b", text, re.M) for text in sources.values())
            if not found:
                dangling.append(f"{rule_id} -> {pointer}")
        self.assertEqual(dangling, [], "IMPLEMENTED pointers lead nowhere: " + "; ".join(dangling))

    def test_no_rule_is_silently_downgraded(self) -> None:
        """Downgrading a mode is forbidden: ENFORCED never becomes CHECKED."""
        self.assertEqual(rule("R2").mode, ENFORCED)
        self.assertEqual(rule("R8").mode, ENFORCED)
        self.assertEqual(rule("R29").mode, ENFORCED)
        self.assertEqual(rule("R30").mode, ENFORCED)
        enforced = [item for item in RULES if item.mode == ENFORCED]
        self.assertGreaterEqual(len(enforced), 17)


class EnforcedRuleTests(unittest.TestCase):
    def test_r2_codex_app_task_api_is_absent_from_production(self) -> None:
        """R2: only the dispatcher creates tasks, through the App Server."""
        forbidden = ("create_thread", "send_message_to_thread", "fork_thread", "handoff_thread")
        offenders: list[str] = []
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                # Only App Server thread/start is allowed; we look specifically for
                # Codex App tool calls.
                for match in re.finditer(rf"\b(codex_app[^\n]*\b{name}|{name}\s*\()", text):
                    line = text[: match.start()].count("\n") + 1
                    if name == "create_thread" and "thread/start" in text.splitlines()[line - 1]:
                        continue
                    offenders.append(f"{path.name}:{line}:{name}")
        self.assertEqual(
            offenders,
            [],
            "the Codex App task API is forbidden by R2: creation goes "
            "only through the deterministic dispatcher and App Server "
            "thread/start",
        )

    def test_r8_self_acceptance_is_rejected_by_plan_validation(self) -> None:
        """R8: a canonical task does not accept itself."""
        data = {
            "schema_version": 3,
            "goal": "g",
            "user_request": "u",
            "model_strategy": "auto",
            "execution_strategy": "serial",
            "max_parallel_workers": 1,
            "computer_use_slots": 1,
            "roles": [
                {
                    "id": "builder",
                    "name": "Builder",
                    "responsibilities": ["build"],
                }
            ],
            "tasks": [
                {
                    "id": "A",
                    "title": "Task A",
                    "objective": "o",
                    "definition_of_done": ["d"],
                    "role": "builder",
                    "execution_mode": "code",
                    "execution_mode_reason": "files suffice",
                    "reasoning": "medium",
                    "verification": {"policy": "self", "required": True},
                }
            ],
        }
        with self.assertRaises(ValueError) as caught:
            validate_plan(canonicalize_plan(data), "adaptive")
        self.assertIn("R8", str(caught.exception))

    def test_r8_exempts_a_migrated_v08_plan(self) -> None:
        """A migrated v0.8 plan predates verification and stays serial."""
        legacy = {
            "schema_version": 2,
            "goal": "g",
            "model_strategy": "auto",
            "milestones": [
                {
                    "title": "t",
                    "objective": "o",
                    "definition_of_done": ["d"],
                    "execution_mode": "code",
                    "execution_mode_reason": "files suffice",
                    "reasoning": "medium",
                }
            ],
        }
        # Migration is proven by the run being migrated: a fresh
        # project does not admit a v0.8 plan at all.
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-v08-input-") as temp:
            state_dir = Path(temp)
            (state_dir / "plan.json").write_text(
                json.dumps(legacy), encoding="utf-8"
            )
            (state_dir / "run-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "run_id": "existing-v08-run",
                        "status": "DONE",
                    }
                ),
                encoding="utf-8",
            )
            plan = validate_migrating_plan(
                legacy,
                "adaptive",
                state_dir=state_dir,
            )
        self.assertTrue(plan.legacy_serial)
        self.assertEqual(plan.tasks[0].verification.policy, "self")


class ContextOrderTests(unittest.TestCase):
    def test_r17_rules_come_before_specifications_and_are_not_truncatable(self) -> None:
        """R17: rules load before specifications and are not truncated."""
        block = rules_for_prompt()
        self.assertEqual(len(block), len(RULES))
        # ENFORCED come first.
        modes = [item["mode"] for item in block]
        self.assertEqual(modes, sorted(modes, key=lambda m: 0 if m == ENFORCED else 1))
        # Every entry carries an id, a mode and a statement.
        for item in block:
            self.assertTrue(item["id"] and item["mode"] and item["rule"])

    def test_r17_violation_history_raises_a_rule_in_priority(self) -> None:
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-rules-"))
        checked_before = [
            item["id"] for item in rules_for_prompt(state_dir) if item["mode"] == CHECKED
        ]
        target = checked_before[-1]
        self.assertNotEqual(target, checked_before[0])

        record_violation(state_dir, target)
        record_violation(state_dir, target)

        checked_after = [
            item["id"] for item in rules_for_prompt(state_dir) if item["mode"] == CHECKED
        ]
        self.assertEqual(
            checked_after[0], target, "a violated rule rises within its own mode"
        )
        # The modes do not mix: ENFORCED stay above.
        modes = [item["mode"] for item in rules_for_prompt(state_dir)]
        self.assertEqual(modes, sorted(modes, key=lambda m: 0 if m == ENFORCED else 1))

    def test_r17_rules_block_precedes_task_contract_in_the_worker_prompt(self) -> None:
        """The order is checked on the actual envelope, not on intent."""
        from codex_autopilot.ai_studio import AIStudioRuntime

        order = list(AIStudioRuntime.build_prompt.__code__.co_consts)
        # The envelope is built as a literal: "rules" must come before "task".
        source = (SRC / "ai_studio.py").read_text(encoding="utf-8")
        envelope = source.split("envelope = {", 1)[1]
        self.assertLess(
            envelope.index('"rules"'),
            envelope.index('"task"'),
            "the rules block must come before the task specification",
        )
        self.assertIn("not truncatable", source)


class EscalationTests(unittest.TestCase):
    def test_r13_escalation_requires_a_reason_from_the_closed_list(self) -> None:
        """R13: the user is not pulled in without a reason code."""
        from codex_autopilot.pipeline_engineer import (
            AuthorizationTopologyError,
            EscalationReason,
            IncidentPhase,
            escalate_to_user,
        )

        incident: dict = {"phase": "DEGRADED"}
        with self.assertRaises(AuthorizationTopologyError) as caught:
            escalate_to_user(incident, "ПОТОМУ ЧТО", at="t")
        self.assertIn("R13", str(caught.exception))
        self.assertEqual(incident["phase"], "DEGRADED", "incident untouched on refusal")

        escalate_to_user(
            incident, EscalationReason.RECOVERY_EXHAUSTED, at="t", detail="исчерпано"
        )
        self.assertEqual(incident["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(incident["escalation_reason"], "RECOVERY_EXHAUSTED")
        self.assertEqual(incident["escalation_detail"], "исчерпано")

    def test_r13_no_direct_phase_assignment_bypasses_the_reason_code(self) -> None:
        """Assigning the phase directly, around the function, is a defect."""
        source = (SRC / "pipeline_engineer.py").read_text(encoding="utf-8")
        body = source.split("def escalate_to_user", 1)[1]
        after = body.split("\ndef ", 1)[1] if "\ndef " in body else ""
        self.assertNotIn(
            'incident["phase"] = IncidentPhase.ESCALATE_TO_USER.value',
            after,
            "escalation happens only through escalate_to_user",
        )


class ProjectPlacementTests(unittest.TestCase):
    """R6 and section 4a: a directory mismatch is found before the worker."""

    def _global_state(self, roots: list[str]) -> Path:
        import json

        home = Path(tempfile.mkdtemp(prefix="codex-home-"))
        (home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "local-projects": {
                        "proj-1": {
                            "id": "proj-1",
                            "name": "Test",
                            "rootPaths": roots,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_r6_preflight_rejects_a_target_outside_desktop_root_paths(self) -> None:
        from codex_autopilot.project_association import (
            ProjectAssociationError,
            require_desktop_project_root,
        )

        target = Path(tempfile.mkdtemp(prefix="codex-target-"))
        other = Path(tempfile.mkdtemp(prefix="codex-other-"))
        home = self._global_state([str(other)])

        with self.assertRaises(ProjectAssociationError) as caught:
            require_desktop_project_root(home, "proj-1", target)
        message = str(caught.exception)
        # The message must name BOTH paths: otherwise the diagnosis is useless.
        self.assertIn(str(target.resolve()), message)
        self.assertIn(str(other.resolve()), message)

    def test_r6_accepts_only_a_target_that_is_one_of_the_declared_roots(self) -> None:
        """A target below a root used to be accepted here (nesting).

        Desktop files a thread in the project only when its cwd EQUALS a
        root (desktop_sidebar): a run started below the root passed preflight
        and every thread it made was in no project. Now preflight refuses
        what the runtime would record as an R5 defect.
        """

        from codex_autopilot.project_association import (
            ProjectAssociationError,
            require_desktop_project_root,
        )

        root = Path(tempfile.mkdtemp(prefix="codex-root-")).resolve()
        nested = root / "work" / "project"
        nested.mkdir(parents=True)
        home = self._global_state([str(root)])
        self.assertEqual(require_desktop_project_root(home, "proj-1", root), (root,))
        with self.assertRaisesRegex(ProjectAssociationError, "equals a root"):
            require_desktop_project_root(home, "proj-1", nested)

    def test_r6_is_reachable_from_the_production_preflight(self) -> None:
        """R19: the function existing is not enough, a call path is needed."""
        source = (SRC / "preflight.py").read_text(encoding="utf-8")
        self.assertIn("require_desktop_project_root(", source)
        self.assertIn("PreflightError", source)
        # The refusal must happen BEFORE the task is created.
        checked = source.index("require_desktop_project_root(")
        created = source.index("start_thread(")
        self.assertLess(
            checked, created, "the rootPaths check must precede thread creation"
        )


class CausalCreationTests(unittest.TestCase):
    """R1 on the journal: creation must follow the owner's completion."""

    def _state(self, journal: list[dict], *, own_threads: tuple[str, ...] = ()):
        """The run's own threads are declared explicitly.

        An owner that belongs to no session is a human thread:
        arm/resume creates a reservation out of a turn the autopilot
        does not watch and whose completion it does not record.
        Without that split the audit declared a break on every resume.
        """

        from codex_autopilot.run_state import RunState

        state = RunState(run_id="r1")
        state.lifecycle_journal = journal
        state.worker_sessions = [
            {"thread_id": thread_id} for thread_id in own_threads
        ]
        return state

    def _audit(
        self, journal: list[dict], *, own_threads: tuple[str, ...] = ()
    ) -> list[str]:
        from codex_autopilot.lifecycle import audit_creation_causality

        threads = own_threads or tuple(
            str(item.get("relay_owner_thread_id") or "")
            for item in journal
            if item.get("relay_owner_thread_id")
        )
        return audit_creation_causality(self._state(journal, own_threads=threads))

    def test_r1_chain_with_a_completed_owner_is_clean(self) -> None:
        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "thread-a"},
        ]
        self.assertEqual(self._audit(journal), [])

    def test_r1_owner_that_never_completed_is_reported(self) -> None:
        """The shape of the real defect: the owner named itself, ran nothing.

        That is how M9 was created in a live run - the owner recorded
        was a thread that appears in the journal under no event of its
        own.
        """

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "outsider"},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("R1", violations[0])
        self.assertIn("outsider", violations[0])

    def test_r1_creation_with_an_empty_owner_is_reported(self) -> None:
        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": None},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("no relay owner", violations[0])

    def test_r1_events_predating_the_field_are_not_assessed(self) -> None:
        """Old runtime records carry no field at all - not a violation.

        _append_event always writes the key, so its absence means a
        different schema version, not a missing owner.
        """

        from codex_autopilot.lifecycle import creation_causality_coverage

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A"},
            {"sequence": 2, "event": "create_requested", "task_id": "B"},
            {"sequence": 3, "event": "turn_completed", "thread_id": "thread-c"},
            {"sequence": 4, "event": "create_requested", "task_id": "C", "relay_owner_thread_id": "thread-c"},
        ]
        self.assertEqual(self._audit(journal), [])
        assessed, total = creation_causality_coverage(self._state(journal))
        self.assertEqual((assessed, total), (1, 3))

    def test_r1_a_user_thread_owner_is_the_documented_path(self) -> None:
        """arm/resume creates a reservation out of a human turn.

        The autopilot does not watch such a turn and never writes
        turn_completed for it at all. The audit used to call this a
        break, and a live run picked up a false mark on every resume.
        """

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "человек"},
        ]
        self.assertEqual(self._audit(journal, own_threads=("воркер",)), [])

    def test_r1_audit_is_reachable_from_the_production_status(self) -> None:
        """M11-R1-REACHABILITY: the audit was called only from the tests.

        It existed, it was exported from lifecycle, and nothing in
        production called it - so the claim "the causality chain is
        checked" rested on nothing. The status report calls it now,
        and the blind zone is named as a number.
        """

        source = (SRC / "status.py").read_text(encoding="utf-8")
        self.assertIn("audit_creation_causality(", source)
        self.assertIn("creation_causality_coverage(", source)

    def test_r1_status_names_both_the_result_and_the_blind_zone(self) -> None:
        from codex_autopilot.status import _render_creation_causality

        clean = _render_creation_causality(
            {"assessed": 3, "total": 3, "violations": []}
        )
        self.assertIn("3/3", clean)
        self.assertIn("no break found", clean)

        broken = _render_creation_causality(
            {"assessed": 2, "total": 5, "violations": ["R1: create_requested #7 ..."]}
        )
        self.assertIn("2/5", broken)
        self.assertIn("1 break(s)", broken)
        self.assertIn("#7", broken)

    def test_r1_dropping_the_field_after_it_appeared_is_a_violation(self) -> None:
        """Otherwise the rule is bypassed by no longer writing the field."""

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B"},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("dropped relay_owner_thread_id", violations[0])


class PlacementHonestyTests(unittest.TestCase):
    def test_r5_project_id_is_never_reported_as_sidebar_placement(self) -> None:
        """R5: "projectId is set" and "visible in the project" differ.

        Passing the first off as the second is why the
        app_server_project_scoped_create event was written honestly
        while the task never appeared in the sidebar.
        """
        source = (SRC / "lifecycle_dispatch.py").read_text(encoding="utf-8")
        self.assertIn("require separate verification", source)
        claim = source.split("project_association_verification", 1)[1][:600]
        self.assertNotIn("sidebar placement verified", claim)
        self.assertNotIn("visible in project", claim)

    def test_r5_status_separates_the_two_namespaces(self) -> None:
        source = (SRC / "status.py").read_text(encoding="utf-8")
        self.assertIn("separate namespace", source)


if __name__ == "__main__":
    unittest.main()
