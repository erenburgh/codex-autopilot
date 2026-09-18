from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.plan import (
    DEFAULT_MAX_PARALLEL_WORKERS,
    Plan,
    validate_plan,
)
from codex_autopilot.run_state import RunState, StateStore, _validate_state
from codex_autopilot.scheduler import (
    SchedulerAvailability,
    compute_ready_task_ids,
    schedule,
)
from codex_autopilot.task_state import TaskState, initial_task_states, transition_task
from _plan_contract import (
    canonical_plan_verification,
    canonicalize_plan,
    canonical_verification,
)


def raw_task(
    task_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    priority: int = 0,
    capabilities: tuple[str, ...] = (),
    execution_mode: str = "code",
) -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": execution_mode,
        "execution_mode_reason": "The declared capability is required.",
        "reasoning": "medium",
        "role": "worker",
        "depends_on": list(dependencies),
        "priority": priority,
        "verification": canonical_verification(),
        "resources": [],
        "required_capabilities": list(capabilities),
        "context": {},
        "outputs": [],
        "tags": [],
    }


def make_plan(
    tasks: list[dict],
    *,
    strategy: str = "parallel",
    max_workers: int = 4,
    computer_use_slots: int = 1,
) -> Plan:
    return validate_plan(
        canonicalize_plan({
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Exercise deterministic scheduling.",
            "user_request": "Exercise deterministic scheduling exactly as specified.",
            "model_strategy": "auto",
            "execution_strategy": strategy,
            "max_parallel_workers": max_workers,
            "computer_use_slots": computer_use_slots,
            "roles": [
                {
                    "id": "worker",
                    "name": "Worker",
                    "responsibilities": ["Complete one task."],
                }
            ],
            "tasks": tasks,
        }),
        "adaptive",
    )


def make_state(plan: Plan) -> RunState:
    return RunState(
        graph_version=plan.graph_version,
        execution_strategy=plan.execution_strategy,
        max_parallel_workers=plan.max_parallel_workers,
        computer_use_slots=plan.computer_use_slots,
        task_states=initial_task_states(plan),
        task_attempts={task.id: 0 for task in plan.tasks},
        task_revisions={task.id: 0 for task in plan.tasks},
        plan_verification=canonical_plan_verification(plan),
    )


def implement(state: RunState, plan: Plan, task_id: str) -> None:
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.RUNNING)
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.IMPLEMENTED)


def verify(state: RunState, plan: Plan, task_id: str) -> None:
    if state.task_states[task_id] == TaskState.READY.value:
        implement(state, plan, task_id)
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.VERIFYING)
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.VERIFIED)


def deferral_reasons(decision, task_id: str) -> tuple[str, ...]:
    """Причины отсрочки задачи - прямо из решения планировщика.

    Раньше это был метод ``SchedulerDecision.reasons_for``. Продакшен не
    звал его ни разу: решение везде читают по ``deferred``. Метод снят как
    вторая дорога к тому же полю, тесты смотрят в поле.
    """

    item = next((item for item in decision.deferred if item.task_id == task_id), None)
    return item.reasons if item else ()


class DependencySchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = make_plan(
            [
                raw_task("A"),
                raw_task("B"),
                raw_task("C", dependencies=("A",)),
                raw_task("D", dependencies=("A", "B")),
                raw_task("E", dependencies=("C", "D")),
            ],
            max_workers=2,
        )
        self.state = make_state(self.plan)

    def test_reference_dag_has_exact_verified_unlock_sequence(self):
        self.assertEqual(compute_ready_task_ids(self.plan, self.state.task_states), ("A", "B"))
        self.assertEqual(schedule(self.plan, self.state).ready_task_ids, ("A", "B"))

        implement(self.state, self.plan, "A")
        decision = schedule(self.plan, self.state)
        self.assertEqual(decision.ready_task_ids, ("B",))
        # The waiting reason is shown by status._waiting_reason: it
        # names both the open dependencies and the resource holder.
        # No second implementation remains in the scheduler.
        from codex_autopilot.task_state import unmet_dependencies

        self.assertEqual(
            {
                task: unmet_dependencies(self.plan, task, self.state.task_states)
                for task in ("C", "D", "E")
            },
            {"C": ("A",), "D": ("A", "B"), "E": ("C", "D")},
        )

        verify(self.state, self.plan, "A")
        self.assertEqual(schedule(self.plan, self.state).ready_task_ids, ("B", "C"))

        verify(self.state, self.plan, "B")
        self.assertEqual(schedule(self.plan, self.state).ready_task_ids, ("C", "D"))

        verify(self.state, self.plan, "C")
        self.assertEqual(schedule(self.plan, self.state).ready_task_ids, ("D",))

        verify(self.state, self.plan, "D")
        final = schedule(self.plan, self.state)
        self.assertEqual(final.ready_task_ids, ("E",))
        self.assertEqual(final.selected_task_ids, ("E",))

    def test_scheduler_rejects_plan_state_graph_race(self):
        self.state.graph_version = 2
        with self.assertRaisesRegex(ValueError, "graph version mismatch"):
            schedule(self.plan, self.state)


class PriorityAndFairnessTests(unittest.TestCase):
    def test_priority_is_explicit_then_critical_path_then_fan_out(self):
        plan = make_plan(
            [
                raw_task("high", priority=100),
                raw_task("long"),
                raw_task("long_child", dependencies=("long",)),
                raw_task("long_leaf", dependencies=("long_child",)),
                raw_task("wide"),
                raw_task("wide_a", dependencies=("wide",)),
                raw_task("wide_b", dependencies=("wide",)),
                raw_task("short"),
                raw_task("short_child", dependencies=("short",)),
            ],
            max_workers=4,
        )
        decision = schedule(plan, make_state(plan))
        self.assertEqual(decision.ready_task_ids, ("high", "long", "wide", "short"))
        scores = {item.task_id: item for item in decision.priorities}
        self.assertGreater(scores["long"].critical_path_length, scores["wide"].critical_path_length)
        self.assertGreater(scores["wide"].transitive_fan_out, scores["short"].transitive_fan_out)

    def test_older_equal_priority_task_wins_independent_of_wall_clock(self):
        plan = make_plan([raw_task("declared_first"), raw_task("older")], max_workers=1)
        state = make_state(plan)
        state.scheduler_sequence = 9
        state.task_ready_since = {"declared_first": 9, "older": 2}

        first = schedule(plan, state)
        second = schedule(plan, state)
        self.assertEqual(first.selected_task_ids, ("older",))
        self.assertEqual(second, first)

    def test_resource_available_tier_runs_before_unavailable_high_priority_work(self):
        plan = make_plan(
            [raw_task("blocked", priority=100), raw_task("available", priority=0)],
            max_workers=1,
        )
        decision = schedule(
            plan,
            make_state(plan),
            SchedulerAvailability(resource_available={"blocked": False}),
        )
        self.assertEqual(decision.ready_task_ids, ("available", "blocked"))
        self.assertEqual(decision.selected_task_ids, ("available",))
        self.assertIn("resource_unavailable", deferral_reasons(decision, "blocked"))


class CapacityAndStrategyTests(unittest.TestCase):
    def test_parallel_and_auto_fill_only_the_bounded_frontier(self):
        for strategy in ("parallel", "auto"):
            with self.subTest(strategy=strategy):
                plan = make_plan(
                    [raw_task("A"), raw_task("B"), raw_task("C")],
                    strategy=strategy,
                    max_workers=2,
                )
                decision = schedule(plan, make_state(plan))
                self.assertEqual(decision.strategy, strategy)
                self.assertEqual(decision.worker_limit, 2)
                self.assertEqual(decision.selected_task_ids, ("A", "B"))
                self.assertIn("worker_capacity", deferral_reasons(decision, "C"))

    def test_serial_is_one_at_a_time_even_with_a_larger_configured_limit(self):
        plan = make_plan(
            [raw_task("A"), raw_task("B"), raw_task("C")],
            strategy="serial",
            max_workers=4,
        )
        decision = schedule(plan, make_state(plan))
        # The point of the test: serial is clamped to one worker under a larger limit.
        # The default constant's own value does not belong here: M10-REV-004
        # requires raising it for new runs.
        self.assertEqual(decision.strategy, "serial")
        self.assertEqual(decision.worker_limit, 1)
        self.assertEqual(decision.selected_task_ids, ("A",))

    def test_active_work_consumes_worker_capacity(self):
        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        state = make_state(plan)
        state.task_states = transition_task(plan, state.task_states, "A", TaskState.RUNNING)
        state.active_task_ids = ["A"]
        decision = schedule(plan, state)
        self.assertEqual(decision.open_worker_slots, 1)
        self.assertEqual(decision.selected_task_ids, ("B",))
        self.assertIn("worker_capacity", deferral_reasons(decision, "C"))

    def test_an_unlimited_account_raises_the_state_cap_instead_of_breaking_it(self):
        """Безлимит снимает потолок - и состояние обязано это пережить.

        На аккаунте с автосписанием бюджет возвращает workers=None, и
        фронтир берёт столько задач, сколько открыл граф. Потолок в
        состоянии при этом оставался прежним, и следующая же проверка
        состояния падала: "active tasks exceed run-state
        max_parallel_workers". Это ValueError, а не DesktopLifecycleError,
        поэтому хук его не ловит - диспетчер умирал, а поверх настоящей
        работы открывался тикет о его падении.

        Лечится не урезанием бюджета: у безлимитного аккаунта число
        воркеров не ограничивается, это решение владелицы. Лечится тем,
        что потолок в состоянии следует за бюджетом.
        """

        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        state = make_state(plan)
        state.rate_limits = {"credits": {"hasCredits": True}}

        decision = schedule(plan, state)

        self.assertEqual(decision.worker_limit, len(plan.tasks))
        self.assertEqual(
            state.max_parallel_workers,
            len(plan.tasks),
            "потолок в состоянии не пошёл за бюджетом",
        )
        self.assertEqual(decision.selected_task_ids, ("A", "B", "C"))

        # The state itself must stay valid with every task taken: this is
        # exactly where the run used to fail.
        state.task_states = {task.id: TaskState.RUNNING.value for task in plan.tasks}
        state.active_task_ids = [task.id for task in plan.tasks]
        _validate_state(state)

    def test_raising_the_cap_is_a_recorded_decision_not_a_silent_repair(self):
        """R22: гейт не вправе молча привести систему в соответствие.

        Поднятый потолок - изменение сохранённого состояния. Без видимой
        записи о том, что именно изменено и на каком основании, это
        самозалечивание, которое правило запрещает прямо.
        """

        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        state = make_state(plan)
        state.rate_limits = {"credits": {"hasCredits": True}}

        schedule(plan, state)

        raised = [
            item
            for item in state.resilience_journal
            if item.get("event") == "worker_cap_followed_budget"
        ]
        self.assertEqual(len(raised), 1, "подъём потолка не записан")
        detail = raised[0].get("detail") or {}
        self.assertEqual(detail.get("from"), 2)
        self.assertEqual(detail.get("to"), len(plan.tasks))
        self.assertIn("unlimited billing", detail.get("reason", ""))

    def test_the_effective_limit_is_one_function_for_every_reader(self):
        """B6: у предела воркеров был три расчёта; теперь один, и он равен решению."""

        from codex_autopilot.scheduler import effective_worker_limit

        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        for limits in ({}, {"credits": {"hasCredits": True}}, {"primary": {"usedPercent": 80.0}}):
            with self.subTest(limits=limits):
                state = make_state(plan); state.rate_limits = limits
                expected = effective_worker_limit(plan, state)
                self.assertEqual(schedule(plan, state).worker_limit, expected)
        serial = make_state(plan); serial.execution_strategy = "serial"
        self.assertEqual(effective_worker_limit(plan, serial), 1)

    def test_a_budget_that_keeps_a_cap_never_exceeds_the_state_cap(self):
        """Инвариант, на котором держится подъём потолка.

        Проверка подъёма стоит под двумя условиями: бюджет снял потолок
        И новый предел больше сохранённого. Второе сегодня выводится из
        первого: все ветки worker_budget, оставляющие потолок, возвращают
        не больше ``declared``, а ``declared = min(plan, state)`` не
        больше сохранённого потолка. То есть условие про None избыточно,
        и мутация, снимающая его, ничего не меняет - я это замерила.

        Раз так, проверять надо не само условие, а инвариант под ним.
        Если однажды появится ветка бюджета, выдающая больше заявленного,
        упадёт этот тест и назовёт причину - а не тот, кто потом будет
        разбирать, почему потолок поднялся на ограниченном аккаунте.
        """

        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        for limits in (
            {"primary": {"usedPercent": 10.0}},
            {"primary": {"usedPercent": 60.0}},
            {"primary": {"usedPercent": 80.0}},
            {"primary": {"usedPercent": 95.0}},
            {"spendControlReached": True},
            {"rateLimitReachedType": "primary"},
            {},
        ):
            with self.subTest(limits=limits):
                state = make_state(plan)
                state.rate_limits = limits
                decision = schedule(plan, state)

                self.assertLessEqual(
                    decision.worker_limit,
                    state.max_parallel_workers,
                    "бюджет с потолком выдал больше сохранённого предела",
                )
                self.assertEqual(state.max_parallel_workers, 2, "потолок поднят зря")
                self.assertEqual(
                    [
                        item
                        for item in state.resilience_journal
                        if item.get("event") == "worker_cap_followed_budget"
                    ],
                    [],
                )

    def test_durable_state_can_tighten_a_parallel_plan_to_serial(self):
        plan = make_plan([raw_task("A"), raw_task("B")], max_workers=2)
        state = make_state(plan)
        state.execution_strategy = "serial"
        decision = schedule(plan, state)
        self.assertEqual(decision.strategy, "serial")
        self.assertEqual(decision.selected_task_ids, ("A",))

    def test_named_capability_capacity_is_consumed_greedily_in_priority_order(self):
        plan = make_plan(
            [
                raw_task("gpu_high", priority=2, capabilities=("gpu",)),
                raw_task("gpu_low", priority=1, capabilities=("gpu",)),
                raw_task("cpu"),
            ],
            max_workers=3,
        )
        decision = schedule(
            plan,
            make_state(plan),
            SchedulerAvailability(
                available_capabilities=frozenset({"gpu"}),
                capability_limits={"gpu": 1},
            ),
        )
        self.assertEqual(decision.selected_task_ids, ("gpu_high", "cpu"))
        self.assertIn("capability_capacity:gpu", deferral_reasons(decision, "gpu_low"))

    def test_active_task_consumes_its_named_capability_capacity(self):
        plan = make_plan(
            [
                raw_task("active_gpu", capabilities=("gpu",)),
                raw_task("waiting_gpu", capabilities=("gpu",)),
                raw_task("ordinary"),
            ],
            max_workers=3,
        )
        state = make_state(plan)
        state.task_states = transition_task(
            plan,
            state.task_states,
            "active_gpu",
            TaskState.RUNNING,
        )
        state.active_task_ids = ["active_gpu"]
        decision = schedule(
            plan,
            state,
            SchedulerAvailability(capability_limits={"gpu": 1}),
        )
        self.assertEqual(decision.selected_task_ids, ("ordinary",))
        self.assertIn(
            "capability_capacity:gpu",
            deferral_reasons(decision, "waiting_gpu"),
        )

    def test_missing_capability_does_not_block_unrelated_work(self):
        plan = make_plan(
            [raw_task("special", capabilities=("device:lab",)), raw_task("ordinary")],
            max_workers=2,
        )
        decision = schedule(
            plan,
            make_state(plan),
            SchedulerAvailability(available_capabilities=frozenset()),
        )
        self.assertEqual(decision.selected_task_ids, ("ordinary",))
        self.assertIn(
            "capability_unavailable:device:lab",
            deferral_reasons(decision, "special"),
        )

    def test_computer_use_slot_is_independent_from_worker_limit(self):
        plan = make_plan(
            [
                raw_task("gui_a", execution_mode="computer_use"),
                raw_task("gui_b", execution_mode="computer_use"),
                raw_task("code"),
            ],
            max_workers=3,
            computer_use_slots=1,
        )
        decision = schedule(plan, make_state(plan))
        self.assertEqual(decision.selected_task_ids, ("gui_a", "code"))
        self.assertIn(
            "capability_capacity:computer_use",
            deferral_reasons(decision, "gui_b"),
        )

    def test_ready_age_survives_atomic_state_round_trip(self):
        plan = make_plan([raw_task("A"), raw_task("B")])
        state = make_state(plan)
        schedule(plan, state)
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-scheduler-state-"))
        store = StateStore(state_dir)
        store.save(state)
        loaded = store.load()
        self.assertEqual(loaded.scheduler_sequence, 2)
        self.assertEqual(loaded.task_ready_since, {"A": 1, "B": 2})


if __name__ == "__main__":
    unittest.main()
