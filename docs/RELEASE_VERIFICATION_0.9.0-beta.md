# Codex Autopilot v0.9.0-beta — independent release verification

Audit task: `M10`  
Audit date: 2026-09-11  
Decision: **REVISE — not release-ready**

This is a fresh-context audit of the checked-out source, Git diff, deterministic
tests, and canonical run state. It does not use implementer transcripts,
implementer self-assessments, or `HANDOFF.md` prose as evidence.

## 1. Starting state and v0.8 baseline

- Repository: the canonical target supplied to M10.
- Branch: `release/0.8.0-beta`.
- HEAD: `76c4b6c`; worktree was already extensively dirty and was preserved.
- Current declared versions remain `0.8.0b0` in `pyproject.toml`,
  `0.8.0-beta` in `src/codex_autopilot/__init__.py`, and
  `0.8.0-beta+codex.20260911012326` in the adaptive plugin manifest.
- The checked-in HEAD v0.8 plan is a schema-2 ordered `milestones` list. Its
  `DesktopOrchestrator` creates one App Server thread at a time and advances a
  cursor. Its existing `ProjectMemory` is project-local SQLite/FTS5 with WAL,
  evidence, verified Truth, typed records, and backups. The candidate correctly
  extends that subsystem instead of introducing a second memory store.

The candidate diff adds schema-3 tasks/roles, scheduler, task state machine,
resources, AI Studio context construction, independent-verification lifecycle,
plan evolution, recovery, status, project association, thread titles, and
Desktop lifecycle modules. These are real implementations, not only design
documents, but the release contract is not yet fully met.

## 2. Architecture found

The candidate architecture is:

```text
structured schema-3 plan
  -> validated DAG and durable RunState
  -> deterministic READY/priority/resource scheduler
  -> fresh implementation reservation(s)
  -> IMPLEMENTED
  -> policy verification/revision lifecycle
  -> VERIFIED dependency unlock
  -> selective verified Project Memory + dependency outputs
```

`AIStudioRuntime` is stateless: it rebuilds bounded prompts from the task, role,
DoD, selected verified memory, dependency outputs, issues, and resources. Model
routing remains capability-based (Sol for code/general work, Astra for Computer
Use), while reasoning is separate. There is no resident controller LLM.

The selected filesystem design is one shared working tree with declared
resource locks and `git.auto_commit=false` by default. Scheduler choice,
retries, locks, reservations, graph changes, and recovery are deterministic
code.

## 3. Critical acceptance matrix

Evidence types are explicit: **deterministic** means source inspection plus local
tests/fakes; **live** means an actual Codex/Desktop/App Server production run.

| Gate | Result | Evidence and boundary |
| --- | --- | --- |
| A. DAG | PASS (deterministic) | `plan.py` schema 3 has explicit dependencies, roles, verification, priority, resources, outputs, capabilities, and context. Initial/change validation rejects cycles and invalid references. |
| B. Parallel execution | PARTIAL | Scheduler and Desktop reservation fakes admit a bounded independent frontier. No live parallel Sol production run was executed. Default new-plan settings remain serial/one worker. |
| C. Verified dependencies | PASS for required verification; policy gap | `IMPLEMENTED` never unlocks a required dependency. The gate is overly strict for `required=false` and cannot express successful self/deterministic acceptance correctly. |
| D. Independent verification | PASS (deterministic) | Fresh verifier reservations, bounded independent context, strict PASS/REVISE protocol, revision loop, and downstream blocking are covered. |
| E. Deterministic verification | **FAIL** | Passing exhaustive checks remain `IMPLEMENTED` and always reserve a verifier LLM. The state machine rejects `IMPLEMENTED -> VERIFIED`. |
| F. Resource safety | **FAIL** | Declared resource conflicts and crash-safe locks pass, but all parallel workers must write the same unclaimed `HANDOFF.md` and share one hash baseline. |
| G. Computer Use safety | PASS (deterministic), live NOT TESTED | Explicit slot default is one; two GUI tasks serialize while a code task continues. No live concurrent GUI attempt was made. |
| H. Fresh sessions | PASS (deterministic) | Each implementation/verifier/revision/replanner reservation has fresh identity; role objects hold no session state. |
| I. Project Memory | PASS (deterministic) | Existing v0.8 store is reused. WAL, `BEGIN IMMEDIATE`, transactional IDs/links, rollback, backups, integrity, concurrent evidence/record/verification/conflict writes pass. |
| J. No controller LLM | PASS (source) | Scheduler, lock coordinator, lifecycle, recovery, and status are ordinary Python. Semantic planning/replanning is a bounded fresh turn. |
| K. Project association | **FAIL** | All payload cwd/workspace roots use the canonical target, but absent a target saved project, preflight wrongly falls back to the initiating task's unrelated saved project. |
| L. Deterministic naming | **FAIL** | Titles are deterministic and bounded, but do not match the required `Role | ID | Purpose` formats or verifier/revision/planner wording. |
| M. Sidebar readability | **FAIL / live NOT TESTED** | Wrong title formats are asserted by old tests; actual parallel Desktop sidebar titles were not observed. |
| N. Serial compatibility | PASS (deterministic) | Schema-2 milestones become an explicit chain DAG pinned to serial/one worker, and legacy state/config migration is fail-closed. |
| O. Security | PASS (deterministic/source) | `:workspace` is enforced; production approvals fail closed; deterministic commands use argv with `shell=False`; memory MCP has no shell/network/raw-SQL proxy; no automatic push/tag/release/reset/clean was introduced. |
| P. Simple UX | **FAIL** | The skill can derive structure from a goal, but its example forces serial/one worker and its exact `start-skill` command omits `--worker-surface desktop_owned`, whose CLI default is headless. |

## 4. Deterministic evidence

### Pre-audit suite

The first unmodified candidate run completed successfully and reported 260
tests with four documented legacy transport skips. During M10, other existing
uncommitted source/tests changed in the shared directory; the final snapshot
therefore contains more tests and was re-run rather than assuming the first
number remained authoritative.

### Focused implementation suites

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_task_graph tests.test_scheduler tests.test_resources \
  tests.test_memory_concurrency tests.test_plan_evolution \
  tests.test_verification_lifecycle tests.test_workspace_ux \
  tests.test_ai_studio tests.test_desktop_lifecycle \
  tests.test_pipeline_engineer

Ran 143 tests in 2.246s — OK
```

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_release tests.test_hook_trust tests.test_preflight \
  tests.test_mcp tests.test_install

Ran 36 tests in 1.964s — OK
```

These prove the current implementation's internal contract; they do not waive
conflicts with the original acceptance request.

### Independent v0.9 contract and scenario tests

M10 added `tests/test_v09_acceptance_contract.py`. Its three distinct synthetic
AI Studio structures pass:

- A: two independent implementation branches, then integration;
- B: research, analysis, then fact verification;
- C: code plus two Computer Use tasks with one GUI slot and a separately routed
  subjective verifier.

Five exact contract probes expose release blockers:

| Probe | Actual result |
| --- | --- |
| schema-3 omitted execution strategy defaults to `auto` | FAIL: actual `serial` |
| `start-skill` defaults to `desktop_owned` | FAIL: actual `headless_app_server` |
| exact implementation/verifier/revision/planner/replanner title shapes | FAIL: actual middle-dot phase format |
| unrelated initiating saved project is not used for target placement | FAIL: initiating project is returned |
| deterministically proven task can transition to `VERIFIED` | ERROR: unconditional `IllegalTaskTransition` |

Final full command:

```text
PYTHONPATH=src python3 -m unittest discover -s tests
```

Result on the audited snapshot: **273 tests in 5.274s; 4 failures, 1 error, 4
skips**. All five red/error outcomes are the contract probes above. The full
deterministic suite therefore does not satisfy M10's completion gate.

## 5. Actionable revision items

### M10-REV-001 — implement real verification-policy semantics (P0)

`task_state.dependency_state_satisfies()` accepts only `VERIFIED`,
`validate_transition()` rejects every `IMPLEMENTED -> VERIFIED`, and
`lifecycle._reserve_followup_sessions_in_state()` reserves a verifier for every
implemented task. Passing deterministic checks are only prechecks.

Required revision:

1. resolve `self`, `deterministic`, `independent`, and `auto` to distinct,
   evidence-gated paths;
2. promote exhaustive deterministic PASS without a verifier model;
3. retain deterministic failure -> structured revision;
4. reserve fresh verifiers only for independent/auto-selected independent work;
5. honor `required=false` in dependency satisfaction without conflating an
   implementer claim with verified Truth; and
6. replace existing tests that assert the non-conforming all-independent rule.

### M10-REV-002 — implement the exact title contract (P0)

`thread_titles.py` currently renders `Role · Implement T44 · Title`,
`Role · Verify T44 · Title`, `Role · Revise T44-R1 · Title`, `Plan · ...`, and
`Replan ...`. It also does not add `Verifier` to the actual verifier role or
derive the required verification/revision purpose wording.

Implement the exact pipe-separated formats, deterministic revision numbering,
bounded normalization, and planner `PLAN`/`PC-<ID>` forms. Update the older
workspace-UX tests; keep App Server title readback fail-closed.

### M10-REV-003 — remove unrelated initiating-project fallback (P0)

`project_association.resolve_preflight_project()` returns a project matching
the initiating cwd when no saved project contains the target. `preflight.py`
then treats it as valid placement and moves a canonical-target task into that
unrelated project.

Use only a validated target-containing saved project. Otherwise keep the task
unassigned/Recents with canonical cwd and report the exact UI-association
limitation. Retain separate App Server and Desktop project-ID namespaces.

### M10-REV-004 — make v0.9 the normal safe default (P0)

`DEFAULT_EXECUTION_STRATEGY`, default max workers, the adaptive skill example,
and the CLI worker surface currently select serial/one-worker/headless behavior.
The skill's exact command omits the flag required to enter the claimed v0.9
Desktop-owned lifecycle.

Make schema-3 new runs default to `auto` with a conservative useful worker
limit, and make normal `start-skill` use `desktop_owned`. Preserve migrated
v0.8 config/state as explicit serial/headless compatibility rather than changing
old projects implicitly.

### M10-REV-005 — make the required checkpoint parallel-safe (P0)

Every parallel session records the same pre-frontier hash of
`.codex-autopilot/HANDOFF.md`. Completion merely checks that the current hash
differs. One worker can therefore satisfy another's gate, and concurrent writes
can lose updates despite otherwise-disjoint task resources.

Use task-scoped, atomically written checkpoint state (preferred), or serialize
the shared advisory file explicitly. Add a two-worker regression proving each
completion supplies its own checkpoint and cannot overwrite or impersonate the
other.

### M10-REV-006 — prevent a retired retry task from continuing beside its replacement (P0)

During the audit, canonical `run-state.json` showed the original M10 reservation
in `RETRY_WAIT` while a new M10 reservation became `ACTIVE`. The interrupted
Desktop task remained user-addressable and continued changing the same shared
working tree. Source/test mtimes changed while the audit was running.

Recovery must reconcile or fence the old Desktop task before a replacement for
the same task can own mutable resources. A stale/retired task that receives new
user input must fail closed rather than continue production beside the active
attempt. Add a deterministic replay test for this exact causal sequence.

### M10-REV-007 — replace legacy live acceptance with v0.9 coverage (P1)

`scripts/live_acceptance.py` constructs schema-2 serial milestones and runs the
legacy `DesktopOrchestrator`. It cannot prove parallel Sol, Sol+Astra overlap,
v0.9 Computer Use slots, fresh independent verifier/revision threads, exact
v0.9 project placement, or new titles.

After the P0 fixes, add an explicitly authorized v0.9 Desktop-owned harness and
record actual task IDs, cwd/project/title readback, turn overlap, model routes,
resource waits, verification rejection, revision, dependency unlock,
Pause/Resume, and crash recovery. Do not auto-approve production requests.

### M10-REV-008 — finish version/release work only after acceptance (P1)

The code, plugin manifest, installer paths, changelog baseline, and
`scripts/build_release.py` still identify v0.8. No v0.9 source ZIP, release ZIP,
or SHA-256 was produced, appropriately, because the acceptance suite is red.
Version and artifacts belong after the P0 revision and a fresh M10 re-audit.

## 6. Detailed behavior inventory

- **DAG schema:** complete and strict; no implicit list-order dependencies for
  schema 3. Cycle checks run on initial and replacement graphs.
- **State machine:** durable WAITING, READY, RUNNING, IMPLEMENTED, VERIFYING,
  REVISION_REQUIRED, REVISING, RETRY_WAIT, VERIFIED, BLOCKED, FAILED, and
  CANCELLED states. `IMPLEMENTED != VERIFIED` is enforced.
- **Scheduler:** stable READY reconciliation; explicit priority, critical path,
  fan-out, logical age, resource availability, declaration order; bounded
  worker/capability slots; no model call.
- **Parallelism:** deterministic reservations and disjoint-resource fake turns
  pass. Configured candidate default is one worker; live overlap is NOT TESTED.
- **Roles:** arbitrary planner-defined profiles; roles do not select models.
- **AI Studio:** bounded fresh phase prompts, no transcript input, verified
  memory and direct verified dependency outputs only.
- **Verification:** fresh independent verifier, strict protocol, and
  REVISE/revision/fresh-verifier loop pass. Policy selection is the P0 gap.
- **Routing:** code -> Sol and Computer Use -> Astra under Adaptive AUTO;
  verifier route may differ. `REQUIRE_COMPUTER_USE` preserves task identity and
  reacquires resources in deterministic tests.
- **Memory concurrency:** SQLite WAL, busy timeout/retry, immediate write
  transactions, transactional ID creation, foreign-key links, integrity check,
  backup/recovery, and simultaneous record/evidence/verification/conflict writes
  pass. Independent verifier agreement alone does not create Truth.
- **Resources:** normalized path/directory/glob and named resources; read/read
  sharing; read/write, write/write, and exclusive conflicts; owner/task/attempt/
  thread/turn identity; unknown recovery remains locked.
- **Computer Use:** explicit independent capacity, default one, and safe Sol
  progress while a second GUI task waits pass deterministically.
- **Plan evolution:** typed prerequisite/dependency/resource/verification
  requests, bounded fresh replanner, graph revalidation, cycle rejection, and
  crash-safe two-file redo pass.
- **Pause/Resume:** drain semantics preserve active locks; resume reconciles
  before admission in deterministic tests.
- **Rate limits:** shared barrier and per-task retry state are deterministic;
  independent active work is preserved in tests. A complete live reset window
  is NOT TESTED.
- **Target root:** cwd and `runtimeWorkspaceRoots` propagation plus metadata
  attestation pass with fakes. Saved-project fallback is the P0 gap.
- **Serial compatibility:** legacy plans remain a chain and never opt into
  parallelism automatically.
- **Status UX:** semantic Running/Verifying/Waiting/Ready groups, progress,
  worker capacity, Computer Use capacity, wait reasons, and active desired title
  pass deterministic tests. Actual sidebar readability is NOT TESTED.

## 7. Security audit

The production source has no `danger-full-access`, sandbox bypass, automatic
approval, unrestricted filesystem MCP, arbitrary-shell MCP, automatic
push/tag/release, or forced Git cleanup path. Configuration requires
`:workspace`. Deterministic check commands are arrays passed with
`shell=False`. The one memory MCP operation union validates project confinement
after symlink resolution and exposes no network, process control, or raw SQL.

`git.auto_commit=true` remains an explicit opt-in compatibility feature; the
default is false. The developer-only live harness has narrow opt-in session
approval helpers and is not production behavior. M10 neither enabled those
helpers nor ran production App Server work.

## 8. Live and release status

NOT TESTED in M10, by design and explicit task restriction:

- two real parallel Sol workers;
- real Sol+Astra overlap;
- actual Computer Use serialization on one desktop;
- live independent rejection -> fresh revision -> fresh verifier -> unlock;
- live multi-worker Pause/Resume and crash/lock recovery;
- actual Desktop project/sidebar association from an initiating task outside the
  target;
- actual implementation/verifier/revision/planner title readback and sidebar
  readability;
- account usage/cost of parallel verification; and
- packaged v0.9 install on a clean Mac.

No release artifacts were prepared. The current builder would still create
v0.8-named artifacts. Therefore there are no v0.9 artifact paths or SHA-256
values to report.

Current source install command (still v0.8-labelled and not a v0.9 release):

```text
./install.sh --profile adaptive --install-deps
```

Intended first-use prompt after the blockers are fixed:

```text
Use Codex Autopilot for /absolute/path/to/project.

Goal: <project goal>
```

## 9. Mechanical M10 changes

M10 changed documentation and tests only:

- added the seven required canonical documents that were absent;
- corrected documentation that described the Stop relay and verification policy
  inconsistently;
- added the independent contract/scenario test module; and
- added this report.

No runtime implementation, commit, tag, push, publish, reset, clean, automatic
approval, or production App Server action was performed by this audit.

## 10. Repair branch re-audit — 2026-09-12

### 10.1 Verdict and audited snapshot

**REVISE — the repair branch is not release-ready.**

This pass independently rechecked the repair claims against production source
and named tests. Commit messages, the preceding sections of this report, and
worker statements were not used as proof.

- Branch at entry: `repair/m10-p0`.
- HEAD at entry: `3dc21b1`; the worktree was clean.
- While this audit was in progress, an external process advanced the same
  branch to `42aaa80`. Those changes were preserved and inspected rather than
  reverted. The final source/test verdict and full suite below are bound to
  `42aaa80`.
- Declared versions still remain `0.8.0b0`, `0.8.0-beta`, and
  `0.8.0-beta+codex.20260911012326` in `pyproject.toml`, the package, and the
  adaptive plugin manifest respectively.

The full deterministic suite passes, but several claimed rule checks either
do not run on every production path or accept the state only after a worker
could already have produced side effects. In accordance with R29, green tests
are admission to this independent judgment, not a substitute for it.

### 10.2 Repair-item disposition

| Earlier item | Disposition | Actual source and named-test evidence |
| --- | --- | --- |
| `M10-REV-001` | **CLOSED BY CONTRACT CHANGE, not by the formerly requested implementation** | R29 now explicitly forbids verifier-free promotion even after exhaustive deterministic checks. `V09ContractRegressionTests.test_deterministic_policy_still_requires_the_verifier` confirms that the current lifecycle still requires a verifier. The earlier finding is retained here rather than silently erased. |
| `M10-REV-002` | **CLOSED (deterministic); live title readback NOT TESTED** | `thread_titles.task_phase_thread_title()` and `replanner_thread_title()` produce the required implementation, verifier, revision, planner, and replanner forms. `_build_descriptor()` uses them in the production reservation path; `create_desktop_thread_via_app_server()` writes the name and rejects a different `thread/read` name. Named tests: `V09ContractRegressionTests.test_thread_titles_match_the_required_human_readable_shapes`, `ThreadTitleTests.test_exact_phase_titles_are_human_readable_and_stable`, and `DesktopLifecycleTests.test_dispatcher_preserves_app_server_project_metadata_before_turn`. |
| `M10-REV-003` | **CLOSED (deterministic); live Desktop association NOT TESTED** | `resolve_preflight_project()` has no initiating-project fallback: it accepts an explicit target-containing project, chooses the unique longest target-root match, or returns no project and keeps canonical cwd/Recents. Named tests: `V09ContractRegressionTests.test_unrelated_initiating_project_is_not_a_target_fallback`, `PreflightTests.test_initiating_project_is_never_a_placement_fallback`, and `PreflightTests.test_target_longest_root_project_is_sent_and_verified_with_canonical_cwd`. |
| `M10-REV-004` | **PARTIAL — OPEN** | Schema-3 defaults are now `execution_strategy="auto"` and `max_parallel_workers=2`, and the `start-skill` CLI defaults to `desktop_owned`; named tests `test_auto_is_the_default_product_execution_strategy` and `test_start_skill_defaults_to_the_desktop_owned_v09_runtime` pass. However, the required product entry point, `plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md`, still instructs the planner to write `execution_strategy:"serial"` and `max_parallel_workers:1`. A normal skill-created run therefore bypasses the repaired code defaults. |
| `M10-REV-005` | **CLOSED (deterministic)** | `task_checkpoint_path()` isolates `.codex-autopilot/handoff/<task-id>.md`; every reservation captures that task's own hash and completion rejects an unchanged task checkpoint. Named regression: `DesktopLifecycleTests.test_parallel_workers_cannot_satisfy_each_others_checkpoint`. |
| `M10-REV-006` | **PARTIAL — OPEN** | `fence_superseded_sessions()` marks the old same-task session `RETIRED_SUPERSEDED`, and authoritative completion from it fails. Named regression: `DesktopLifecycleTests.test_superseded_desktop_task_fails_closed_beside_its_replacement`. The test does not exercise its stated precondition: it calls completion directly after the hypothetical input. The installed `UserPromptSubmit` hook calls `handle_prompt_hook()`, which immediately returns `{}` for every ordinary prompt before looking up the session. A retired, still-addressable thread can therefore execute and mutate the shared tree; only its eventual completion is rejected. This does not satisfy the pre-side-effect fence required by R24. |
| `M10-REV-007` | **NOT TESTED** | No live App Server/Desktop run was authorized by this code-only M10 contract. Therefore parallel Sol, Sol+Astra overlap, real Computer Use serialization, live verifier/revision/unlock, actual project/sidebar placement, and actual title readback remain unobserved. Fakes and unit tests are not relabelled as live evidence. |
| `M10-REV-008` | **NOT TESTED** | Release packaging/install was deliberately not run because its live acceptance prerequisite (`REV-007`) has no evidence and P0 issues remain. The source still declares v0.8, so producing or accepting a v0.9 ZIP/SHA-256 would be premature. No release artifact, tag, push, or publish was performed. |

### 10.3 Claimed rule-enforcement audit

| Rule | Result | Production reachability and named test |
| --- | --- | --- |
| R1 | **PARTIAL — OPEN** | The reservation/retry path carries and validates the causal relay owner; `test_worker_completion_binds_new_frontier_to_that_worker_thread`, `test_nonnull_foreign_retry_owner_without_causal_worker_fails_closed`, and `test_first_legacy_task_retry_preserves_its_bound_initiator_owner` pass. But `audit_creation_causality()` and `creation_causality_coverage()` are only exported and called by tests; no production status, preflight, completion, or recovery path invokes them. The advertised machine audit is dead code under R19. |
| R5 | **PARTIAL — OPEN** | `launch_checklist()` now reads Desktop state, and `DesktopVisibilityTests.test_a_thread_missing_from_desktop_records_is_reported` passes. It searches only for a thread ID anywhere in the Desktop maps, not for association with the canonical target project, and never tests editability. `test_invisibility_is_reported_but_never_becomes_a_ticket` explicitly keeps a known-invisible task at `IN_PROGRESS`; after `_launch_report()`'s single bounded wait this yields `continue` and no infrastructure ticket. `CREATED_EVENTS` also permits plain `app_server_thread_created` while the check is labelled `created_in_project`. The rule is observable but not enforced. |
| R6 | **PARTIAL — OPEN** | Canonical cwd, App Server `projectId`, Desktop `rootPaths`, and post-create metadata mismatches are checked by production preflight/dispatch; `test_r6_is_reachable_from_the_production_preflight` and `test_thread_start_project_metadata_mismatch_fails_closed` pass. However, `AppServerClient.ensure_project_root()` silently calls `project/update` when the root is absent, and `AppServerClientTests.test_project_root_is_added_before_project_scoped_thread_start` requires that mutation. No recorded Project Memory Decision is consulted. This is automatic reconciliation of a detected mismatch, contrary to R6 and R22. |
| R7 | **PARTIAL — OPEN** | `_audit_task_scope()` runs on authoritative completion and named tests `test_change_outside_the_declared_area_is_recorded_on_completion` and `test_change_inside_the_declared_area_is_clean` pass. There is no task time/attempt/token budget in the plan schema or completion gate. In the shared tree, `observe_changed_paths()` returns all changes since the common HEAD plus all untracked files, so two disjoint parallel writers each see the other's paths; `M10-CHECK-R7-PARALLEL-ATTRIBUTION` reproduced mutual false violations through the production audit function. |
| R13 | **PARTIAL — OPEN** | Pipeline incidents use the closed `EscalationReason` enum; `test_r13_escalation_requires_a_reason_from_the_closed_list` and `test_r13_no_direct_phase_assignment_bypasses_the_reason_code` pass. The authoritative worker protocol independently accepts bare `AUTOPILOT_STATUS: BLOCKED` or `ESCALATE`; completion moves the task to `BLOCKED` with free text and no reason code. Headless `CoreTests.test_escalate_at_max_blocks` exercises that bypass. The restriction is not unified across production escalation paths. |
| R16 | **PARTIAL — OPEN** | Standard implementation/revision prompts request `AUTOPILOT_RULES`, and completion records missing or unknown IDs; the `test_r16_report_*` tests pass. The same completion audit also runs for verifier sessions, but the verifier prompt requests only `AUTOPILOT_VERIFICATION` and never requests `AUTOPILOT_RULES`, so a conforming verifier necessarily records an R16 violation. `test_every_prompt_variant_asks_for_the_list` misses this because it selects only prompts containing `AUTOPILOT_STATUS`. Replanner and Pipeline Engineer prompts also omit the structured rule block and applied-rule report, and no machine Conflict path implements the final sentence of R16. |
| R17 | **PARTIAL — OPEN** | The standard `AIStudioRuntime` envelope places structured rules before task/DoD/context and fails if the full prompt exceeds its bound; `test_r17_rules_block_precedes_task_contract_in_the_worker_prompt` and `test_r17_rules_come_before_specifications_and_are_not_truncatable` pass. The production `_replanner_prompt()` starts with phase/request/current plan and contains no rules, while `build_pipeline_engineer_prompt()` also has no rules block. The ordering contract is not applied to every specialist phase. |
| R18 | **PARTIAL — OPEN** | External evidence cannot directly create verified Truth or start a non-user decision as accepted; `ExternalInputTests.test_external_evidence_cannot_support_truth` and `test_decision_resting_on_external_content_cannot_start_accepted` pass. `set_decision_status()` does not recheck origin/evidence, so an external-backed proposed decision can immediately become accepted; `add_constraint()` likewise creates an active external-backed constraint. `M10-CHECK-R18-TRUST-TRANSITIONS` reproduced both results (`decision_status=accepted`, `constraint_status=active`). External evidence also does not require provider/source provenance. |
| R21 | **CLOSED (deterministic)** | The complete suite passed with `CODEX_THREAD_ID`, `CODEX_TURN_ID`, and `CODEX_SESSION_ID` removed. `CleanEnvironmentTests.test_no_test_module_reads_session_scoped_environment`, `test_frontier_reservation_is_imported_through_the_explicit_helper`, and `test_production_environment_reads_stay_declared` constrain the remaining declared production reads. |

R29 and R30 are now declared `ENFORCED`, but the repository's own
`tests/test_rules_contract.py` still lists both in `PENDING`. R29's current
no-verifier-free-acceptance behavior is covered elsewhere, as noted above.
R30 has no department field, versioned department rubric in Project Memory, or
fresh lead-derived verifier selection. Its required acceptance-title form also
conflicts with the original v0.9 verifier-title form implemented for
`REV-002`; this contract conflict must be recorded/resolved rather than guessed
by an implementation worker.

### 10.4 Incident signatures and two-level recovery

**Normalized incident signatures: CLOSED (deterministic).**
`incident_signature()` derives identity from version, code, normalized surface,
operation, and side-effect outcome rather than task/signal IDs or prose. Named
tests `test_the_same_failure_under_different_signal_ids_shares_a_signature`,
`test_the_same_failure_on_another_task_shares_a_signature`,
`test_free_text_never_changes_the_signature`, and
`test_recurrence_is_counted_under_one_signature` cover the actual store path.

**Two-level recovery: PARTIAL — OPEN.** The journal/state transitions, retry
budget, one recovery slot, repeated-fix promotion, and Pipeline Engineer phase
exist, and `test_a_repeated_fix_becomes_a_runbook_and_skips_the_engineer` plus
`test_exhausted_budget_hands_over_to_the_engineer` pass. But
`attempt_known_recovery()` executes no runbook action and runs no system probe:
it derives `safe` only from stored classification/side-effect metadata, then
constructs a passing `HealthcheckResult` from those same fields and marks the
incident `RECOVERED`. `test_known_failure_is_recovered_without_an_engineer`
asserts that state transition without asserting a repair side effect. Learned
`actions` have no deterministic executor on this path. This is a simulated
recovery, not evidence-backed level 1, and violates R22.

### 10.5 Deterministic commands and adversarial probes

Clean-environment full suite at final audited HEAD `42aaa80`:

```text
env -u CODEX_THREAD_ID -u CODEX_TURN_ID -u CODEX_SESSION_ID \
  PYTHONPATH=src python3 -m unittest discover -s tests

Ran 379 tests in 7.703s
OK (skipped=4)
```

The four skips are the explicitly decorated legacy App Server slot-reuse
regressions in `tests/test_core.py`; they are not newly hidden failures.

Focused repair/rule suite:

```text
env -u CODEX_THREAD_ID -u CODEX_TURN_ID -u CODEX_SESSION_ID \
  PYTHONPATH=src:tests python3 -m unittest \
  tests.test_v09_acceptance_contract tests.test_workspace_ux \
  tests.test_preflight tests.test_desktop_lifecycle \
  tests.test_rules_contract tests.test_declared_scope \
  tests.test_rule_contract_and_external_input tests.test_clean_environment \
  tests.test_incident_signatures tests.test_launch_gate

Ran 189 tests in 3.117s
OK
```

Adversarial checks used production functions rather than changing source:

- `M10-CHECK-REV006-PRESUBMIT`: ordinary input with a stale session ID passed
  through `handle_prompt_hook()` as `{}`.
- `M10-CHECK-R7-PARALLEL-ATTRIBUTION`: a common changed-path observation for
  two tasks with disjoint declared directories produced one out-of-scope
  violation for each task, each naming the other task's path.
- `M10-CHECK-R18-TRUST-TRANSITIONS`: external evidence backed a proposed
  project decision; `set_decision_status()` accepted it and `add_constraint()`
  made an external-backed constraint active.

The passing suite therefore proves that the checked-in tests agree with the
implementation. It does not refute the uncovered paths above.

Project Memory evidence recorded by exact check role:
`M10-CHECK-CLEAN-SUITE` = `EVID-060`,
`M10-CHECK-FOCUSED-CLAIMS` = `EVID-061`,
`M10-CHECK-REV006-PRESUBMIT` = `EVID-062`,
`M10-CHECK-R7-PARALLEL-ATTRIBUTION` = `EVID-063`, and
`M10-CHECK-R18-TRUST-TRANSITIONS` = `EVID-064`.

### 10.6 Remaining actionable work for M11

1. Change the adaptive skill's schema-3 planning template to safe useful
   `auto`/two-worker defaults while retaining migrated v0.8 serial behavior.
2. Fence a retired Desktop session at `UserPromptSubmit` (or make it
   unaddressable) before model/tool side effects; add a hook-level replay test.
3. Make R5 verify the thread's exact target-project association and
   editability, and convert a stable post-deadline mismatch into a normalized
   infrastructure incident rather than perpetual `IN_PROGRESS`.
4. Make target project-root drift fail closed unless a recorded user Decision
   authorizes `project/update`; never self-heal and continue silently.
5. Add task-attributed change tracking for shared-tree parallel scope audits
   and structured time/attempt/token budgets with fail-closed enforcement.
6. Route every user escalation, including worker `BLOCKED`/`ESCALATE`, through
   a required closed reason code.
7. Apply the structured rule contract and ordering to verifier, replanner, and
   Pipeline Engineer prompts; require applied-rule reporting where completion
   audits it and implement R16 Conflict recording.
8. Revalidate trust on every transition to accepted Decision/active Constraint
   and require external-source provenance before such evidence can influence
   control state.
9. Replace synthetic level-1 recovery with an allowlisted action executor and
   an independent real healthcheck; never mark `RECOVERED` from incident
   metadata alone.
10. Implement R30's department-derived fresh lead, versioned department rubric
    in Project Memory, stable two-attempt rubric loading, and resolve its title
    conflict explicitly.
11. After the P0 fixes, execute `REV-007` with a real authorized App Server and
    Desktop, then and only then version/package/install-check `REV-008` and
    record artifact hashes.

### 10.7 Security and mutation audit

Production source and the adaptive plugin contain no `danger-full-access`,
sandbox bypass, automatic approval, unrestricted filesystem/shell MCP, silent
permission escalation, or automatic Git push/tag/release path. App Server
thread creation retains `:workspace`, allowlisted params, and explicit
project/cwd metadata checks. This M10 pass changed only this report and its
required task-scoped handoff; it did not alter or revert repaired code, run a
production App Server, or create a commit, tag, push, publication, reset, or
clean operation.
