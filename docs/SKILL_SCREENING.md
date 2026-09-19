# Hiring · Skill screening

Codex Autopilot decides which skills a worker carries at the moment that worker
is hired, not when the plan is written. A plan cannot know: it is authored
before anyone has looked at the repository the task will touch, and the
capability a task needs is a property of the work, not of the graph.

This document describes the screening layer that produces a skill stack per
task and feeds it into the resolver that already exists in
`src/codex_autopilot/skill_packs.py`.

## The measured gap

On 20 Sep 2026, against a plan with no `skill_packs` field, the assembled
implementation prompt for a task was inspected directly
(`AIStudioRuntime.build_prompt`, phase `implementation`):

```text
plan.skill_packs        = ()
role.skill_requirements = ()
task.loaded_skills      = ()
prompt carries          "loaded_skills":[]
```

Every worker in that run received an empty stack: no procedures, no
checklists, no failure modes, no quality criteria beyond its own definition of
done. `resolve_skill_stack` is not broken — it is fed nothing. Its only catalog
source is `plan.skill_packs` (`ai_studio.py:313`), and its only selection source
is `task.loaded_skills`, a plan field no planner fills, because no planner can.

So the layer that is missing is not a resolver. It is the hiring decision that
resolver was built to consume.

## Where the step runs

Today, one reservation pass does this:

```text
Stop hook / wake
  -> require_trusted_stop_hook_for_config      (hook trust)
  -> CODEX_THREAD_ID -> relay_owner_thread_id  (ownership guard)
  -> ResourceLockCoordinator.transaction()
       load_plan, StateStore.load
       _reserve_in_state
         engineer -> plan verifier -> replanner -> follow-ups -> frontier
           _build_descriptor -> _worker_prompt -> AIStudioRuntime.build_prompt
             resolve_skill_stack(...)   <- the stack is frozen into the prompt here
       StateStore.save
  -> _materialize(descriptors)          (launches/<token>.json)
dispatch
  -> App Server thread/start            (the worker thread is created)
  -> App Server turn/start              (prompt + the Autopilot skill)
```

The decisive fact: **the prompt, and therefore the skill stack, is baked into
the launch descriptor inside a synchronous, model-free, lock-held transaction.**
Nothing in `_reserve_in_state` can ask a model anything. A screening step that
reasons about the task cannot run there.

Therefore screening runs **one reservation earlier**. It is a session of its
own, the way the Pipeline Engineer is a session of its own: reserved by
`_reserve_in_state`, created as a real Codex thread through App Server,
completed through the ordinary completion path. Its structured answer lands in
run state. The next frontier pass finds an answer already recorded for that
task and builds the worker prompt from it.

```text
task becomes READY
  -> no hiring record for (task, graph_version)?
       reserve kind="screening"   (no work slot, no resource locks, no state change)
       -> the screener answers    AUTOPILOT_SCREENING: {...}
       -> the requisition is recorded in run state
  -> next frontier pass
       reserve kind="implementation"
       -> the recorded decision resolves into the stack
       -> the stack enters the prompt through resolve_skill_stack
```

In code: `lifecycle_screening.screening_gate`, called from the frontier loop
in `lifecycle_reservations._reserve_in_state` before the implementation
reservation, and `lifecycle_screening.complete_screening_session`, dispatched
from `lifecycle_completion.complete_desktop_worker` by session kind.

## Whether a run screens at all

`runtime.skill_screening` in `config.toml`:

| mode | when a task is screened |
| --- | --- |
| `never` | never — **the default** |
| `auto` | when the plan or the installed library holds at least one pack |
| `always` | every task, including with an empty library |

The default is off for the same reason `desktop_notifications` is off: one
screening is one more Codex thread per task out of the user's limits, and
spending them is the user's decision, not a default. `auto` is the setting
most projects want — with nothing to hire from, a screening turn can only
answer "nothing available", so `auto` costs nothing until a pack exists and
starts working the moment one does. `always` is for a project that wants its
unmet needs on record from the first run, which is the intake for qualifying
new skills.

### What the screener may see at that moment

* The task contract: objective, definition of done, execution mode, role,
  declared resources, required capabilities, its verification contract.
* The goal contract and the user request — what the project actually is.
* The **inventory**: which skill packs exist on this machine and in what state
  (trusted and qualified, candidate, or merely present).
* Prior hiring records for this run and how those tasks then went.

### What it may not see, and may not do

* It has no worker thread to inspect — that thread does not exist yet. That is
  the whole point of running before creation.
* It takes no resource lock and no worker slot, and changes no task state. It
  is not production work; a screener waiting on the lock its own worker will
  need would deadlock the frontier.
* It writes no production artifact and produces no evidence that could later
  count as verification of the work. **No self-acceptance**: the thing that
  hires a worker never judges that worker's output.
* It cannot answer an approval, cannot bypass hook trust or the ownership
  guard, and reaches App Server through the same path every other session
  does. There is no second creation path.

### Screening may never stop the core

The owner's constraint is explicit: the predictable part — a task is created,
handed over, runs to DONE — is the core, and skills are superstructure. So the
failure policy is asymmetric:

* **Fail-closed on skills.** A skill that cannot be resolved, is not trusted,
  or is not qualified does not enter the prompt. Ever, silently, under any
  pressure.
* **Fail-open on the task.** A screening session that errors, times out, or
  returns an unreadable answer does not block the task. The task is reserved
  unscreened, with an empty stack, and the run records that it ran unscreened
  and why.

`MAX_SCREENING_ATTEMPTS` (two: the first try, and one more after a refusal
whose reason the screener can read) enforces the second half. Without it, a
screener that always fails would hold the frontier forever — which is exactly
the failure mode the constraint forbids. After the ceiling the task is
reserved unscreened and `task_hiring[<task>].unscreened` says why.

## The requisition

The screener's answer is one final structured line, the same protocol shape as
`AUTOPILOT_VERIFICATION` and for the same reason: free-form reasoning may
precede it, the protocol line must occur exactly once and be the last non-empty
line, so that a quoted example or an abandoned draft cannot advance durable
state.

```text
AUTOPILOT_SCREENING: {"task_id":"M3","items":[ ... ]}
```

Each item is a requisition for one capability:

| field | meaning |
| --- | --- |
| `capability` | the capability the worker needs, in the catalog's namespace |
| `rationale` | why **this task** needs it, in terms of the task |
| `necessity` | `required` or `helpful` |
| `candidates` | exact `{id, version}` references the screener believes satisfy it, best first; may be empty |
| `search_intent` | what to look for if nothing installed satisfies it; only meaningful when `candidates` is empty or none resolve |

`rationale` is not decoration. It is the record against which the choice is
later judged, and it is what makes a hiring decision auditable rather than an
opaque list. An item without one is refused.

`capability` is the join key because the resolver already refuses two packs
that claim the same capability in one stack (`_reject_stack_duplicates`). One
requisition item therefore yields at most one loaded pack, by construction.

## Resolution

The runtime — not the screener — turns the requisition into a stack. Each item
resolves to exactly one of three outcomes:

| outcome | when | effect |
| --- | --- | --- |
| `hired` | a candidate is in the inventory, is `trusted`, and has an authoritative qualification PASS bound to its exact revision | binds into the prompt |
| `withheld` | a candidate is in the inventory but is `candidate` status, unqualified, or conflicts with an already-hired pack | recorded with the exact reason; does **not** bind |
| `unmet` | nothing in the inventory claims the capability | recorded with the reason and the `search_intent`; does **not** bind |

`withheld` and `unmet` are the honest half. The runtime never pretends a skill
is present. A worker whose requisition was half met is told so: it gets the
skills it actually has, and the run carries the record of what it did not get.

### The inventory

The catalog handed to `resolve_skill_stack` becomes the union of two sources:

1. `plan.skill_packs` — unchanged, still authoritative, still validated by
   `validate_plan_skill_bindings` at plan load.
2. The **installed skill library** on this machine: pack manifests on disk,
   read-only at screening time.

The union is a change to the *catalog argument*, not to the resolver. Every
existing rule still applies to every pack from either source: exact versions,
no duplicate identity, no duplicate capability, no conflicting pair, trusted
status only, and a revision-bound qualification PASS in Project Memory before
anything reaches a prompt.

That last gate has a consequence worth stating plainly, because it constrains
what hiring can deliver on day one: **a skill that has just been discovered
cannot bind to the very next worker.** `resolve_skill_stack` demands a
qualification PASS for the exact revision, and for a `vetted` or
`project_generated` pack it also demands an independent source attestation.
Those are produced by real verification work, not by the act of choosing. A
freshly-found skill therefore enters as `withheld: not yet qualified`, and
qualifying it is the next thing the run does — which is the learning loop
below, not a detour from it.

## Binding

Nothing new reaches the worker through a new channel. The hired references are
passed to the same `resolve_skill_stack` call at `ai_studio.py:313`, and the
same `to_prompt_dict()` puts procedures, checklists, failure modes, quality
criteria and provenance into the `loaded_skills` block of the envelope.

One gate needs widening rather than removing. Today the call passes
`requirements=implementation_role.skill_requirements` — a plan field — so that
nothing loads that was not declared somewhere authoritative. A hired skill is
not in the role's plan-declared requirements, so the requirement set becomes
the union of the role's declarations **and the recorded hiring decision for
this exact task and graph version**. The invariant is preserved in meaning:
nothing loads that no authority declared. The hiring decision is that second
authority, and it is durable, attributable and auditable, not a runtime guess.

## What is recorded

In `run-state.json` under `task_hiring`, keyed by task id and bound to the
graph version the hire was made for, written through `StateStore` inside the
same transaction as every other state change:

* the requisition as the screener returned it, verbatim, including every
  `rationale`;
* the resolution: which items were hired, withheld or unmet, and the exact
  reason for each non-hire;
* the identity of the screening session — thread id, turn id, reservation
  token — so the decision has a causal author;
* whether the task ran unscreened, and why, if it did.

Joined later with the task's own verdict — PASS, REVISE, how many revisions,
how many re-hires — this is the outcome record: *this stack, chosen for these
reasons, produced this result.*

## The learning loop

The existing `PromotionEvidence` machinery is built for exactly this, and the
loop closes through it rather than through anything new:

1. A skill is hired and the task passes. That PASS is a `verified_work_outcome`
   in Project Memory — one of the promotion evidence kinds
   `skill_packs.py` already accepts.
2. A pack sourced as `learned` needs `independent_verification` **and**
   `verified_work_outcome` before it can be declared trusted
   (`_validate_learned_promotion`). The first comes from a verification session
   that is not the session that did the work. The second comes from step 1.
3. `validate_trusted_skill_promotions` then re-resolves both against Project
   Memory at load, refusing any claim whose records do not exist, do not bind
   the exact revision sha256, or are not cited by an independent PASS.

So a skill earns trust by being used and having the result independently
verified — and the promotion is refused if the same session tried to do both.
The screening layer contributes the first half of that record; it is never the
authority for the second.

A `withheld: not yet qualified` item is the intake for this loop: it names a
pack that something wanted, in a task that needed it, with a rationale. That is
a better queue for qualification work than any list a human would maintain.

## Source trust — the open decision

Obtaining a skill that is not on the machine is a supply-chain path into the
user's Codex. It is the owner's decision, not the runtime's, so the obtaining
path sits behind a named seam and currently obtains nothing: a requisition item
that nothing installed satisfies is recorded `unmet` with its `search_intent`,
and no network access, download or install happens.

The options, concretely:

**A — Local only (what is built today).** A skill may come only from the
installed library on this machine, put there by the user. The screener
selects; it never acquires. Recorded: nothing new. Revocation: the user deletes
the manifest. Approval: none needed, because nothing crosses a boundary.
*Cost:* the hiring layer is only as good as what the user installed, and the
model's ability to research what exists is unused.

**B — Synthesis from what the model already knows.** The screener may *author*
a pack — procedures, checklists, failure modes, quality criteria, deterministic
checks — without fetching anything. It enters as `source: "synthesized"`,
`status: "candidate"`, and reaches a worker only after the independent
promotion `skill_packs.py` already demands. Recorded: the full manifest, its
revision sha256, and the session that authored it. Revocation: demote to
candidate, and every dependent binding fails closed at the next prompt
assembly. Approval: a single policy decision, once, because no external content
is ever admitted.
*Cost:* the model writes from memory, which may be stale or wrong — which is
precisely what the qualification checks and the independent promotion exist to
catch.

**C — Fetch from named, allow-listed origins.** The screener may fetch skill
material from origins on a user-maintained allow-list (for example, a
specific set of repositories or registries). Content is admitted as
`source: "vetted"`, `status: "candidate"`, pinned by content digest, and still
requires an independent source attestation plus qualification before binding.
Recorded: origin URL, fetch time, content digest, and the attesting session.
Revocation: remove the origin from the allow-list; every pack from it is
demoted, and all bindings fail closed. Approval: the user approves each
**origin** once, not each skill — approving per skill would put Autopilot in
the position of asking the user to adjudicate things it was hired to decide,
and approving a whole category once would be an open door.
*Cost:* this is the real supply-chain surface. Everything fetched is untrusted
content that a model will read and act on, and prompt injection inside a
fetched skill is a live risk that the digest and the attestation reduce but do
not eliminate.

**Recommendation: build A now, ship B next, and hold C until the owner
explicitly opens it.**

A is already the honest floor. B gives the owner most of what she described —
the model working out what this worker needs and writing it down as a real
pack — without admitting one byte of external content, and the existing
promotion gate already governs it end to end. C is the only option that needs a
new trust boundary, and it should not be opened by inference from "the model
can research this". If she wants C, the per-origin allow-list is the shape to
build, and the fetched-content-is-untrusted rule has to be written into the
screener's own prompt, not assumed.

## What is not built

* Acquisition of any kind (option B or C). A requisition item that nothing
  installed satisfies is recorded `unmet` with its `search_intent`, and no
  network access, download or install happens. That is the seam, and it
  currently denies.
* Attaching a hired skill as a real Codex `{"type":"skill"}` turn input beside
  Autopilot's own. `turn/start` takes a list and today receives exactly one
  such item (`appserver.py`); whether App Server accepts several has not
  been measured, and nothing here depends on it. The measured, working binding
  path is the prompt envelope.
* Joining a hire with the task's eventual verdict. The record carries the
  choice, its grounds and its author; nothing yet reads it back alongside
  PASS/REVISE counts to say which choices paid off. The `unmet` and
  `withheld` entries are already the queue for qualification work.
