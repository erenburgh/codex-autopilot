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
