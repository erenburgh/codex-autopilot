# Verification and revision lifecycle

Verification is a deterministic lifecycle decision, not an interpretation of
an implementer's confidence. An implementation or revision worker can only
produce `IMPLEMENTED`. A task whose policy still requires work then remains
dependency-ineligible while local checks run or a fresh verifier is active.

## Policy matrix

| Policy | Required behavior after `IMPLEMENTED` |
| --- | --- |
| `self` | The implementer performs and evidences an ordinary self-check; no second LLM is created solely for this policy. |
| `deterministic` | Exhaustive declared checks run locally. A failure creates structured revision issues; an all-pass result reaches `VERIFIED` without a verifier LLM. |
| `independent` | Always create a new verifier task. Declared local checks never short-circuit this policy. |
| `auto` | Select deterministic verification when coverage is sufficient, self-check for low-risk work, or a fresh independent verifier for important/subjective/high-impact work. |

A dependency unlocks on one state and no other: `dependency_state_satisfies`
(`src/codex_autopilot/task_state.py`) returns true only for `VERIFIED`.
`required=false` does not change that, and an evidence-backed `IMPLEMENTED`
result never satisfies a dependency. Implementer-authored tests are evidence,
never a substitute for the original request or Definition of Done.

The matrix above describes policies the runtime does not offer for a canonical
task: `_validate_canonical_acceptance` (`src/codex_autopilot/plan.py`) refuses
`self`, `deterministic` and `auto` outright under R8/R29, so
every canonical task reserves a fresh independent verifier. The page is kept
for the vocabulary it defines; the behaviour to rely on is the paragraph above
this one. There is no outstanding regression behind it - the suite is green.

## Deterministic checks

Checks run in declaration order in the canonical project directory:

- `command` passes an argument vector directly to `subprocess`, never to a
  shell, enforces its timeout, and compares the exact exit code.
- `artifact` resolves its path under the canonical project root and rejects an
  escaping path. Existence is the deterministic assertion.
- `evidence` requires a current implementation evidence record whose
  milestone-evidence `role` exactly equals the check ID. This gives the
  otherwise prose-free evidence declaration a stable slot name.

Command and artifact results are recorded as task-linked Project Memory
evidence. A failed check becomes a structured issue with code
`CHECK-<check-id>`, expected state, actual state, and bounded output.

## Department lead (R30)

Every acceptance is a department lead's. The department is the worker's
profession: every task of one `role` names the same lead in
`verification.verifier_role`, never its own role, and that lead is the
verifier of every task of the profession (`department_runtime`). A submitted
plan or plan change that leaves a lead out, gives one profession two leads or
makes a profession its own lead is refused, every such task in one list; a
task a change leaves untouched is not asked for a lead of its own. The lead of
a profession is the one with work of it still to accept: a task already
`VERIFIED` or `CANCELLED` keeps the lead that judged it and takes no part in
"one lead" (`settled_task_ids`), at admission and at run time. A plan from
before R30 could name several leads for one profession, and a `VERIFIED` task
cannot change - counting those refused every later plan change of such a run
and left the profession's other tasks without a lead for good (found by the
independent check). A task still to be accepted takes part, touched by the
change or not. The exemption is history, not licence: a settled task is left
out only as the current plan holds it, and once a profession's work has been
accepted its lead is one of those that accepted it or the one the current plan
names for the rest - never a new one. The second check reproduced the gap: M01
`VERIFIED` by `art-reviewer`, and a change moving M03 to a new lead whose only
expectation was "Anything goes." passed with no issue; the new department would
have started from a fresh rubric version 1 built from a profile the replanner
wrote - a new standard with no outcome evidence. Accepted means `VERIFIED` by a lead that could lead today (`_can_lead`): the third
check reproduced a run from before R30 whose accepted character-artist work had
no `verifier_role`, or its own role, or `legacy-worker` - no change could pass,
since its "accepted lead" was `'None'` or the profession itself, and the stop
went to her. Such work, and a `CANCELLED` task judged by no one
(`SettledTasks.accepted`), locks nothing: the planner names the lead. A task already under way may
be given only a lead its profession already names (`reconcile_plan_change_state`). A saved plan is never refused on load: a task with no lead is
found by the run's roster before any task starts (`staffing`), and the run
does not start - one ticket of kind `staffing` through the one stop door holds
every task not yet settled (and any task a plan change adds while it is
open); once the run is under way such a ticket holds only the tasks the
roster leaves unstaffed, and the other departments go on to acceptance -
with the full list for the on-call, who has
the plan changed so the whole roster assembles (`requires_roster`); the
verifier's own gate (`department_gate`, stop kind `department_lead`) stays
the backstop for a rubric history that turns ambiguous later. The on-call has
the plan changed to name the lead - naming the lead of a task already under way is not
a rewrite of its work, and resets nothing (`resilience.names_only_its_lead`).
Nothing of the department is written into `plan.json`: the plan digest bound
to PLAN_VERIFIED does not move (checked on the live beyondness plan). A
declared `departments` entry only names a department; its `rubric` field, if a
0.13 plan carried one, is kept verbatim and is never the pin.

The rubric belongs to the department and lives in Project Memory, in a scope
no model may write (`department-acceptance-rubric:<id>`). Version 1 is the
runtime's, derived from the lead's `verification_expectations` (or
responsibilities) and a fixed core - fidelity to the request, every DoD item
closed by evidence, independent re-checking - with no text of any one run's
request. It is written before the first task (bootstrap), after a committed
plan change, and under the coordinator lock when a verifier is reserved - the
last lands it on a run already under way. A changed lead profile does not
rewrite it: the change is recorded once and shown to the lead. A later
version is proposed only by the department's lead or the on-call, from its own
thread (`codex-autopilot department-rubric-propose`), with outcome evidence -
evidence an acceptance of the department rested on, as the runtime recorded it
from another lead's thread, and not written by the proposer (Project Memory's
audit knows the writer's thread). No model door changes a rubric record's
status - a dispute made v1 vanish from the history and come back as a second
v1. A stray record in the scope makes the history ambiguous; the canonical
history is the first record of each version 1..n, the on-call supersedes what
is outside it (`devops-supersede-rubric`) and returns the task.

The lead's thread is titled `<Lead Role> | Verify <Task ID> | <Short Task
Title>`; its prompt carries `department_acceptance` - the department, the
rubric's exact reference and content - and its verdict attests the reference
exactly (a third field, `rubric`). A verdict without it, or attesting another
rubric, is a recorded refusal and a fresh lead, never an incident. A refusal
that is not the lead's mistake - the rubric advanced while it judged, or it was
launched by a runtime from before R30 - is recorded and does not count toward
the three that stop a task. Every `runtime.second_lead_every`-th acceptance of
a department (default 5) is judged again by a second fresh lead on the same
work and rubric; the first verdict is applied, the second is recorded beside it
with the department's disagreement rate (`department_audit`). A lead thread
that received a turn after its acceptance is found by thread/read in the
wake-up and the periodic sweep - for a finished or paused run too, since a lead
is read ten minutes after it ends - and recorded as an R30 defect; only a foreign
turn still running there is a ticket, and it holds no task.

## Independent verifier boundary

The verifier is a newly reserved Desktop task with a new reservation token,
operation ID, worker sequence, and thread. Its selective prompt contains only:

- the task ID, title, objective, numbered Definition of Done, and the lead
  of its department with the department's rubric (`department_acceptance`);
- the immutable original user request and run goal in a dedicated acceptance
  gate;
- the verifier execution capability and its declared reason;
- IDs plus structural selectors for evidence produced by the immediately
  preceding implementation or revision; and
- deterministic results only when a policy explicitly supplies them.

It contains no implementer final response, implementer self-assessment,
verifier transcript, `HANDOFF.md` prose, or concurrent conversation. The
verifier retrieves the selected evidence from Project Memory and records new
independent evidence before its result can be accepted.
It independently compares the result with the original request, task contract,
and every DoD item. Tests written by the implementer are evidence only; they
cannot define, weaken, or waive acceptance criteria.

The final non-empty verifier line is one strict JSON protocol record:

```text
AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[],"rubric":{"record_id":"FACT-001","version":1,"sha256":"<64 hex>"}}
```

or:

```text
AUTOPILOT_VERIFICATION: {"verdict":"REVISE","issues":[{"code":"ISSUE-1","summary":"short issue","details":"specific evidence and correction","dod_refs":[1]}],"rubric":{"record_id":"FACT-001","version":1,"sha256":"<64 hex>"}}
```

The parser rejects duplicate or non-final markers, unknown keys, malformed
issues, `PASS` with issues, and `REVISE` without issues. Free-form prose can
precede the protocol line but is never forwarded to another worker.

## Revisions

`REVISE` performs `VERIFYING → REVISION_REQUIRED`. If the configured limit is
available, one atomic reservation increments `task_revisions[task_id]` and
performs `REVISION_REQUIRED → REVISING`. Revision `R<n>` is a new Desktop task
whose prompt contains the original task contract and structured issues only.
It never receives the verifier transcript.

A successful revision returns to `IMPLEMENTED` and re-enters the same policy.
An independent policy therefore creates fresh verifier `V<n+1>` rather than
reusing the prior verifier thread. Exhausting `max_revision_attempts` re-hires
the task up the effort ladder (see `REHIRING.md`); at the top the task moves to
`BLOCKED` through the stop door - a ticket to the on-call, reserved in the same
completion next to independent work - and it never unlocks dependents. Three
unreadable verdicts and an unroutable verifier also go through the door, but
they are infrastructure (R3): the task stays `IMPLEMENTED`, held by its ticket,
and a fresh verifier comes when the ticket closes. It becomes `BLOCKED` only
when the on-call hands the ticket to the owner, or when the same stop comes
back after the on-call closed it twice (R23: the third goes to the owner with a
report, see `PIPELINE_ENGINEER.md`).

## Routing, resources, and dependency unlock

Verifier model routing follows capability, not role. The verifier uses
`verification.execution_mode` and `verification.reasoning` when provided,
otherwise the task values. In Adaptive `auto`, `code` routes to Sol and
`computer_use` routes to Astra. Host Settings sends no model or reasoning
override.

Every verifier or revision uses the same automatic App Server `thread/start`
and production `turn/start` with canonical cwd and App Server project ID,
resource lock, Computer Use slot, authoritative Stop or Interrupt identity, and
full process exit after completion as implementation work. Follow-up
verification/revision is reserved before unrelated READY work when capacity and
resources permit.

`VERIFIED` satisfies every dependency. `IMPLEMENTED` may satisfy only a
dependency whose verification is explicitly not required. `VERIFYING`,
`REVISION_REQUIRED`, and `REVISING` never satisfy a dependency.
