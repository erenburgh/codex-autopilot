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

`required=false` may allow an evidence-backed `IMPLEMENTED` result to satisfy a
dependency. Independent verification remains mandatory only when the declared
policy selects it. Implementer-authored tests are evidence, never a substitute
for the original request or Definition of Done.

The current candidate does not yet implement this matrix: successful `self`,
passing `deterministic`, and `auto` tasks all reserve a fresh verifier, while
the dependency gate ignores `required=false`. The behavior is covered by a red
independent contract regression and must be revised before release.

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

## Independent verifier boundary

The verifier is a newly reserved Desktop task with a new reservation token,
operation ID, worker sequence, and thread. Its selective prompt contains only:

- the task ID, title, objective, numbered Definition of Done, and selected
  verifier role;
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
AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}
```

or:

```text
AUTOPILOT_VERIFICATION: {"verdict":"REVISE","issues":[{"code":"ISSUE-1","summary":"short issue","details":"specific evidence and correction","dod_refs":[1]}]}
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
reusing the prior verifier thread. Exhausting `max_revision_attempts` moves the
task to `BLOCKED` with a journaled reason; it never unlocks dependents.

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
