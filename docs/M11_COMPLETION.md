# M11 — closed by hand

M11 was called «Run live acceptance and prepare beta artifacts» and was
meant to assemble the **0.9.0-beta release from the 0.8.0 tree**. By the time
its turn came, that had stopped being true: the line had moved on to 0.8.1
and 0.8.2, and the 0.8.0 tree is now known to contain 2 471 lines of dead and
unreachable code. Releasing from it would have meant releasing what we had
just taken apart.

So M11 was closed by hand and on 0.8.2, not run through the pipeline. The run
`repair/m10-p0` it stood in was stopped: everything it could give was taken
from it, and every one of today's fixes was born from its failures.

This is not a bypass of the pipeline. The pipeline was confirmed separately
and cleanly: acceptance ran twice — on 0.8.0 and on 0.8.2 — each time three
milestones, six workers, every task inside the project, zero tickets, DONE.

## Why M11 never launched

Five causes, each found and removed at its own moment:

1. `originator` was `codex_work_desktop` instead of `Codex Desktop` — tasks
   were created invisible.
2. The Stop hook answered `decision: "block"`. A turn whose Stop hook
   answered `block` stays `interrupted` forever, while the dispatcher waits
   for `completed` — the launch never came.
3. Reserving the engineer crashed with `NameError: AIStudioRuntime`: the
   import had been cut by a simplifier as unused.
4. `turn/start` was called on a thread not loaded by that connection.
5. And the last one, against which nothing would have helped: a plan change
   required a verbatim echo of `user_request`. In that run the field was
   35 234 characters. No plan change could pass at all, and M11 could not
   continue without one.

By the fifth point the state of M11 itself already carried the traces of all
the previous ones: a dead thread, an ambiguous session, a spent engineer, a
replanner, a plan change applied by hand. Resurrecting that state made no
sense.

## What of the Definition of Done is fulfilled

| DoD item | State |
| --- | --- |
| All deterministic tests and validators pass | **Yes.** 462 tests, `PYTHONPATH=src python3 -m unittest discover -s tests`. |
| The tree carries the documents and the migration guide | **Yes**, from M10: `MIGRATION_0.8_TO_0.9.md`, `DEPENDENCY_GRAPH.md`, `PARALLEL_EXECUTION.md`, `ROLES.md`, `RESOURCE_LOCKS.md`, `THREAD_NAMING.md`, `PROJECT_ASSOCIATION.md`, `TESTING.md`. |
| Live acceptance of parallel workers and Computer Use | **No.** The 0.8.2 acceptance ran with one worker. Parallelism is checked by a separate clean run — see below. |
| App Server / Desktop metadata verified in every phase | **Yes.** Placement was observed live in all six acceptance workers; `desktop_placement` was rewritten onto `thread/read` and a `projectId` comparison; a discrepancy now reaches a ticket. It was also checked on the live server that the `originator` fix works: every 0.8.2 acceptance thread carries `Codex Desktop` and an empty `threadSource`, while the M11 run's threads carry `codex_work_desktop` and `agent_created_thread`. |
| A final report with 57 items and an exact upgrade guide | **Replaced.** The list of 57 items belonged to the 0.9.0 release from the 0.8.0 tree. In its place — the breakdown of the 12-item set of the independent review, below, and the upgrade guide at the end. |

## Breakdown of the M11 set from the independent review

Numbering from `RELEASE_VERIFICATION_0.9.0-beta.md`, section 11.6.

| # | Item | State |
| --- | --- | --- |
| 1 | `M11-ENTRYPOINT-DEFAULTS` | **Closed.** Both skill templates carry `auto` and two workers; it is stated that parallelism is created by the shape of the graph, not by a flag, and that a shared file serializes siblings through the resource lock. |
| 2 | `M11-PRE-SIDE-EFFECT-FENCE` | **Closed.** The refusal happens on `UserPromptSubmit`, before a single model or tool call. The active session outranks the retired one, control phrases take their own path, an unreadable state does not raise the fence. The repeat is covered by a test. |
| 3 | `M11-R1-REACHABILITY` | **Closed.** `audit_creation_causality` is called from the status report. It is a report, not a ban: the causality barrier decides at the moment of creation; here it is re-checked after the fact from the journal. The blind spot is named as a number. |
| 4 | `M11-R5-DESKTOP-PLACEMENT` | **Closed in substance.** A measured discrepancy fails the verdict and goes into one normalized ticket; an unmeasured state is no longer eternal — 180 seconds after creation it becomes a negative result. Editability remained an observation: `canAcceptDirectInput` arrives `null` both in `thread/read` and in all thirty rows of `thread/list`; a gate cannot be built on such a field. |
| 5 | `M11-R6-FAIL-CLOSED` | **Closed.** `verify_project_root` only reads; a root may be appended only under a found accepted user decision naming this project and this root (`authorize-project-root --yes`, lifted by `--revoke`). The refusal happens before creation and goes into the existing `definitive` branch. Membership is nesting — the same rule by which preflight picks the project. |
| 6 | `M11-R7-ATTRIBUTION-BUDGET` | Open. There is no per-task accounting of the shared tree and no time/attempt/token budgets. |
| 7 | `M11-R13-UNIFIED-REASONS` | **Closed.** `BLOCKED` and `ESCALATE` carry a code from the closed list; an unknown code is rejected, a missing one is recorded as `UNSPECIFIED` and counted as an R13 violation. `ROTATE` and `DONE` have no code and one is rejected with them. |
| 8 | `M11-R16-R17-PHASE-CONTRACT` | Open for the verifier, the replanner and the engineer. |
| 9 | `M11-R18-TAINT` | **Closed.** Of four paths one had been closed. Now: provenance is mandatory on intake, the `external` label appeared in the memory tool schema, a Constraint on external material is rejected, the transition into a binding state re-checks the taint, external support cannot be appended after the fact. The `contradicts` link stays open always. |
| 10 | `M11-REAL-RECOVERY` | Partial. An independent healthcheck is mandatory: `devops-resolve-incident` requires a check name and observations, `RESOLVED` happens only when the ticket is really closed. The list of allowed actions remains prompt text; the actual boundary is set by the set of guarded commands, not by checking the list. |
| 11 | `M11-R30-LEAD-RUBRIC` | Open. There are no discipline leads and no versioned rubric. |
| 12 | `M11-LIVE-AND-RELEASE` | The live part was done twice. Packaging and the SHA-256 record — after a clean run. |

Items 5, 7 and 4 were done separately, after the breakdown: they lie on the
path of task creation and completion, and the changes there were taken one
at a time, with the whole suite run after each. Two things were found right
in the process.

First: a root's membership in a project was determined by equality, not by
nesting. An equality check would count as a discrepancy the ordinary case
where the canonical directory lies inside the project root — and that is
exactly why the previous code appended another root on every new
directory. The trace is visible in the live state: the "Codex Autopilot"
project got the root `.../work/codex-autopilot-v0.8.0-beta` that way.

Second: `visible_in_desktop` was not among the deciding items at all. That
is, a measured `OUTSIDE` or `ABSENT` never opened a ticket — not only in the
unmeasured case, as I believed while reading the justification.

Items 2 and 9 were closed next. Each held a defect that was not in the
audit set.

In the fence: the skill promises the user «ask `статус`», while the hook knew
only the expanded forms like «статус Codex Autopilot». The promised visible
path did not work as written.

In the taint: the `external` label was not listed in the memory tool schema
at all. A worker taking in text from outside could record it only as
`file`, `tool` or `user_instruction` — that is, the taint vanished at the
moment of intake, and all later checks looked at a label nobody could set.
And separately: the memory operation signatures did not make it into the
final schema, remaining dead text.

Open remain 6 (budgets), 8 (the phase contract for the verifier, the
replanner and the engineer), 10 (the allowed-actions list as a check rather
than prompt text), 11 (leads and the rubric) and packaging from 12.

## What checks what M11 did not get to check

A clean run on a new project `work/codex-thread-tools`: four tasks, where the
two middle ones do not need each other's result and are declared siblings on
one dependency. The scheduler, checked on this graph, yields:

```text
step 1: ready=('T1',)        -> LAUNCH ('T1',)
step 2: ready=('T2', 'T3')   -> LAUNCH ('T2', 'T3')
step 3: ready=('T4',)        -> LAUNCH ('T4',)
```

That is, the run checks exactly what no acceptance had checked: two
simultaneous workers, unblocking by dependency, an independent verifier on
the last task, and the revision path if it returns the work.

## Upgrading

From 0.8.1 or 0.8.2: reinstall from the tree, `./install.sh`. The external
launch path stays `current/bin/codex-autopilot`, so hook trust and tool
permissions are not reset.

From 0.8.0: the same, but the plan template changed. Existing runs are not
affected — their `execution_strategy` is recorded in `config.toml` and stays
as it was. A new run enters `auto` with two workers.

From 0.7.x: the state is migrated conservatively and stays serial with one
worker; `legacy_serial` keeps a migrated plan from slipping into parallelism
implicitly.
