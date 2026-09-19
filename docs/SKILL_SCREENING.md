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

## Screening the market

The owner's requirement, in her words: *"Skills are not only inside the system.
Skills are on the internet, on GitHub, wherever people publish them. Using only
the known ones is weak. The market has to be screened."*

So the screening step is not a lookup in a bundled catalogue. It is research at
hiring time: the model goes and finds what would actually help this worker with
this task, and comes back with a specification naming the skills and why each
one.

### A skill here is text, not code

A `SkillPack` is procedures, checklists, failure modes, quality criteria,
required tools, required MCP servers, deterministic checks and evidence roles.
It reaches the worker through `to_prompt_dict()`. Nothing is installed, and the
pack itself does not execute. So "fetching a skill from the market" means
fetching **text** and qualifying it into a pack.

That splits the obtaining path in two, and they are not equally risky:

* **(A) A pack built from fetched text.** No installation, fully recorded,
  revertible by deleting one manifest. This is what the existing machinery was
  built for.
* **(B) A real Codex skill bundle installed into Codex so a worker can invoke
  its scripts.** Third-party code on the machine, colliding with the runtime's
  own plugin-cache and hook-trust discipline. Not built, and behind a named
  seam.

### What the trust ladder already decides — measured, not assumed

Run against `trust.py` and `skill_packs.py` on 20 Sep 2026:

```text
external  ->  external_text / unverified
meets TRUTH_THRESHOLD (what _require_deterministic_trust demands):  False
promote TRUSTED_SKILL on external text alone:  refused
    - supporting evidence is below human_verified: external:external_text/unverified
    - trusted Skill promotion requires evidence of improved outcome
Truth evidence kinds: artifact, build, file, test, tool      (external is not one)

authoritative_documentation backed by kind="external"  ->  REFUSED
authoritative_documentation backed by kind="file"      ->  ACCEPTED
```

The second block is worth stating plainly: `_validate_qualifying_evidence` lists
`external` as an acceptable kind for `authoritative_documentation`, and then
`_require_deterministic_trust`, one line later, refuses every `external` record.
**That branch is unreachable.** Either it is dead code, or it was meant to admit
fetched documentation and the trust gate closed it; either way, nothing today
can promote a pack by citing where its text came from.

So the rule the design must obey — already in the code, not added here:
**the market may suggest; only running the thing and having the outcome
independently verified can make it trusted.**

A market-sourced pack is therefore `source: "synthesized"`, `status:
"candidate"`. Not `vetted` and not `project_generated`: both demand an
independent *source* attestation, which is a claim about origin, and no origin
on the open internet earns that by being fetched. `synthesized` is the honest
label — the screener wrote the pack; the fetched text was its input, never its
authority — and its promotion route already requires `independent_verification`
plus a deterministic test, a real tool, or a verified work outcome.

### The requisition item that names the market

Today an item names candidates from the inventory, or a `search_intent` when
nothing fits. The market extension adds a third form: the screener returns a
**draft pack** together with where it read.

```json
{"capability":"svelte",
 "rationale":"M7 writes Svelte 5 components and this project has no procedure for runes.",
 "necessity":"required",
 "candidates":[],
 "draft":{"id":"svelte-runes","version":"0.1.0","source":"synthesized","status":"candidate", "...":"a full pack manifest"},
 "sources":[{"provider":"github.com/<owner>/<repo>","locator":"docs/runes.md","digest":"sha256:..."}]}
```

What the **runtime** does with it — never the screener:

1. Parse `draft` through `skill_pack_from_raw`, the same validator a
   plan-declared pack passes. A draft that is not a well-formed pack is refused
   and named.
2. Force `status: "candidate"`, and refuse `source` in `{vetted,
   project_generated}` outright — those claim an origin review nobody did.
3. Record each entry of `sources` as Project Memory evidence of kind `external`
   with its `provider` set. The trust ladder requires a provider for external
   evidence and pins the result at `external_text/unverified`, permanently.
4. Write the manifest into the project library — recorded, and revertible by
   deleting that one file.
5. Record the item in the hiring decision as `withheld: newly drafted, not yet
   qualified`. **It does not reach the worker that asked for it.**
6. It is now the intake for a qualification task, which is where the learning
   loop already starts.

### R18: a fetched pack shapes HOW, never WHETHER

External content may influence how work is done and must never become the
authority for whether work is accepted. The surfaces where a market pack could
leak into acceptance, named exactly:

* `build_prompt` resolves the same stack for every phase, so a pack's
  `quality_criteria` and `checklists` reach the **verification** prompt too. A
  market-sourced pack could whisper to the acceptor.
* `require_allowed_skill_evidence_role` widens which evidence roles may be
  written for a task using the loaded packs' `evidence_roles`.
* A pack's `deterministic_checks` are its own qualification and not the task's
  acceptance. That separation already holds and must stay.

The enforcement has a precedent in this repository:
`department_acceptance.omit_conflicting_rubric_guidance` and
`redact_conflicting_rubric_identity` already perform this exact surgery on
superseded rubric identity before a verifier launches. The same seam:

* a pack whose provenance chain includes an `external` source is omitted from
  the **verification**-phase stack, and the verifier is told which capabilities
  the worker carried and that their text was withheld from it — so it is
  informed, not blinded;
* such a pack contributes no `evidence_roles`;
* the acceptance gate and the definition of done are untouched by any pack.

### The sharp edge, which is not the text

`deterministic_checks.argv` is executed. Measured at the call site:

```python
subprocess.run(list(check.argv), cwd=root, capture_output=True,
               text=True, timeout=check.timeout_seconds, check=False, shell=False)
```

and `validate_plan_skill_attestations` requires a qualification task's check to
match the pack's `argv` and expected exit code **exactly**. So a market-sourced
draft that declares `argv` is proposing a command to run on the user's machine,
in the project root. No shell, but any binary the vector names.

There is already a real gate in front of it: the argv reaches execution only
through a plan that contains a matching qualification task, and plans are
written by the replanner and judged by an independent plan verifier. That is a
model-in-the-loop gate, not a deterministic one.

**This, and not the provenance of the prose, is the supply-chain question.**

## The owner's decision

Three questions, sharpest first.

**1. May a market-sourced draft declare its own `deterministic_checks.argv`?**
*Answered: no.* The allowlist is not hand-written in the runtime — it is
derived from what this project already runs, namely the checks its own plan
declares, which for a canonical task is the suite check the acceptance floor
demands. A market pack contributes procedures, checklists, failure modes and
quality criteria, and proves itself against a command the project trusted
before the pack arrived. A draft whose argv is outside that set is not refused
silently: it is recorded as a requisition item that could not be qualified, and
the refusal names what would have been accepted (R31). Not built — it has
nothing to act on until the obtaining path exists.

* *No (recommended).* The runtime refuses a draft whose argv is not drawn from
  a small allowlist the project already trusts — its own test command, its
  linter, its build. A market pack then contributes procedures and quality
  criteria, and proves itself against checks the project already runs. This
  keeps everything the owner asked for and gives up nothing she described.
* *Yes, with approval.* The argv is shown to the user once per exact pack
  revision and runs only after they approve. Honest, but it puts the user in
  the loop of something Autopilot was hired to decide, once per skill.
* *Yes.* The plan verifier is treated as sufficient. I do not recommend it: it
  is a model judging whether a fetched command is safe to run.

**2. May the screening thread reach the network at all?**

Autopilot does not grant network access and must not try to; the screener runs
under the project's `:workspace` permission profile and whatever the user's
Codex settings already allow. The question is whether the screener is *told* to
go and look. Recommended: yes, for reading — that is what screening the market
means, and with question 1 answered "no" the fetched text cannot make itself
trusted or run anything.

**3. Open web, or an allow-list of origins?**

Recommended: **open web for reading.** Once a fetched pack cannot promote
itself and cannot propose a command, an origin allow-list adds friction without
much safety, and it is exactly the "use only the known ones" weakness the owner
objected to. The residual risk it would reduce is prompt injection reaching the
qualification task's context — real, but bounded by that task running only
project-trusted commands and by an independent verifier that never sees the
fetched text.

If she wants an allow-list anyway, the shape is per-origin approval recorded
once, not per-skill, and every pack from a revoked origin demotes to candidate
and fails closed at the next prompt assembly.

## What is not built

* Acquisition of any kind. A requisition item that nothing installed satisfies
  is recorded `unmet` with its `search_intent`, and no network access, download
  or install happens. That is the seam, and it currently denies.
* The argv allowlist of question 1, which has nothing to act on until drafts
  can arrive.
* Installing a real Codex skill bundle so a worker can invoke its scripts.
  That is third-party code on the machine and it collides with the plugin-cache
  and hook-trust discipline; it stays a named seam with nothing behind it.

## What R18 enforcement is built

A pack may declare `external_sources` — `{provider, locator, digest}`, with
`provider` mandatory because the trust ladder refuses external evidence without
one. Declaring it has three consequences, all enforced in code:

* the pack cannot claim `source: "vetted"` or `"project_generated"`; both rest
  on an independent review of the origin, and reading published text is not
  that review;
* `external_sources` is inside `revision_sha256` when present, so changing
  where a pack came from invalidates its recorded verdicts — and it is omitted
  from the digest when absent, so packs already qualified in a live project
  keep the digest their records point at;
* the pack is **withheld from the verification-phase prompt**
  (`ai_studio.build_prompt`). The verifier instead receives
  `withheld_external_skills` naming the capability, the pack and its providers,
  so it knows what the worker carried without reading the text that shaped the
  work — informed, not blinded.
* Attaching a hired skill as a real Codex `{"type":"skill"}` turn input beside
  Autopilot's own. `turn/start` takes a list and today receives exactly one
  such item (`appserver.py`); whether App Server accepts several has not
  been measured, and nothing here depends on it. The measured, working binding
  path is the prompt envelope.
* Joining a hire with the task's eventual verdict. The record carries the
  choice, its grounds and its author; nothing yet reads it back alongside
  PASS/REVISE counts to say which choices paid off. The `unmet` and
  `withheld` entries are already the queue for qualification work.
