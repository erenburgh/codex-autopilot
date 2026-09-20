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
| `always` | every task — **the default** |
| `auto` | only when the plan or the installed library already holds a pack |
| `never` | never |

Hiring ships enabled. The owner chose that over off and over a per-run
ceiling, with the number in front of her: one extra Codex thread per task, and
on her own stalled run — 25 tasks, 25 distinct roles, so no reuse to offset it
— that is 25 extra threads. The condition she attached is that a feature which
is on by default must be able to say what it spent, which is what the status
card line and the hiring record are for.

`auto` is the conservative middle: it screens only once there is something
local to hire from. It made more sense before the market could be screened;
with research at hiring time a screener always has something to do, even if
that is only recording an unmet need.

`initialize_project` writes the mode into `config.toml`, so the file reads
without knowing the defaults and a project can start with hiring off.

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

The same asymmetry has to hold at prompt assembly, which is where it was
nearly lost. `build_prompt` runs *inside* the lock-held reservation
transaction, so an exception there does not spoil one prompt — it fails the
whole frontier pass and leaves the task unreservable. Two ways a hire could do
that, both measured and both now closed:

* **A hire that stops resolving.** The catalog can move between the screening
  that chose a skill and the reservation that builds the prompt — a manifest
  replaced, a pack demoted to candidate. Each hired reference is now resolved
  on its own; one that fails is dropped and reported to the worker in
  `hired_skills_that_no_longer_resolve`, with the resolver's exact reason.
* **A hire too large for the context budget.** A pack's procedures,
  checklists, failure modes and quality criteria have no length bound, and a
  hire is chosen at runtime by a model rather than written into the plan by a
  human. Measured: one pack with 40 000 characters in each of those four
  fields renders as 160 565 characters against a 193 800 budget, so two are
  over on their own — and at 80 000 the refusal was observed directly:
  `implementation prompt for A1 is 334897 characters against a 193800
  budget ... loaded_skills=320764`. Hired skills now get a bounded share of
  the budget (`MAX_HIRED_SKILL_CHARS`), required before helpful, and what does
  not fit is reported in `withheld_for_context_budget`.

Plan-declared skills are deliberately *not* treated this way. They are the
plan's authority, and an oversized or unresolvable one is a plan defect that
should stop loudly. The same goes for a malformed manifest in the installed
library: that is a static configuration error, loud and fixable, not a
model's runtime choice.

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

## Where a skill may come from — answered

The owner rejected text-only, with an example worth keeping: if building a
site needs `Taste`, which is downloaded from GitHub, a user does not get by
with "just text" — the worker needs the skill itself, not a paraphrase of it.
Her boundary: *exactly those things that are Skills, and not plugins or
anything else.*

**A skill may be installed. A plugin may never be.** That line is not
arbitrary — it is what protects the rule that hook trust is never touched. A
plugin registers hooks, MCP servers and commands and lives in the Codex plugin
cache, so installing one changes the host's trust surface. A skill is a
`SKILL.md` bundle: instructions and whatever files ship beside them. Installing
one registers nothing.

### How Codex actually discovers a skill — measured on this machine

Not inferred from how any other tool does it. Two first-party mechanisms,
both measured against codex-cli 0.154.0:

**1. `$CODEX_HOME/skills/<name>`.** Codex ships a system skill,
`~/.codex/skills/.system/skill-installer`, whose own description is: *install
Codex skills into `$CODEX_HOME/skills` from a curated list or a GitHub repo
path*. Its documented behaviour: downloads from `--repo <owner>/<repo> --path
<path>`, installs into `$CODEX_HOME/skills/<skill-name>`, aborts if the
destination exists, and the skill is available on the next turn. It touches no
plugin, no hook, no MCP registration and nothing in the plugin cache.

**2. An explicit path on the turn.** From Codex's own generated protocol
schema (`codex app-server generate-json-schema`), `TurnStartParams.input` is an
array of `UserInput`, one variant of which is:

```json
{"title": "SkillUserInput", "required": ["name", "path", "type"],
 "properties": {"name": {"type": "string"}, "path": {"type": "string"},
                "type": {"enum": ["skill"]}}}
```

The path is arbitrary and the array carries no `maxItems`. Autopilot already
relies on the arbitrary part: it passes its own skill by a path in the install
root, nowhere near `$CODEX_HOME`.

So the boundary and the mechanism agree, and there was no need to stop and
report: neither path goes anywhere near a plugin.

### Which one this runtime uses, and why

**A hired skill bundle lives in the project, at
`.codex-autopilot/hired-skills/<name>@<digest>/`, and the worker is given its
exact path.** Not `$CODEX_HOME/skills`, for four reasons:

* installing into `$CODEX_HOME` is a side effect on the whole machine, for a
  decision made by one task in one project;
* it collides with skills the user installed herself, and the first-party
  installer *aborts if the destination exists* — so a project could not hire a
  skill she already has at a different revision;
* two projects cannot hold different revisions of the same skill;
* reversing it means reaching into her Codex home, while a project-local
  bundle is undone by deleting one directory, which
  `--purge-project-state` already covers.

`hired` is a derived property over the outcomes, not a stored field — it is
`status == "hired" and skill is not None` — which is why the necessity map used
by the context-budget trim cannot go out of step with it: both are built from
the same outcomes, keyed identically, and an outcome carrying no skill is
excluded from `hired` by construction.

One thing is honestly not measured: the schema permits several skill items on
one turn, but whether App Server loads all of them has not been proven by a
live turn. Nothing here depends on it. The worker receives the bundle the way
it already receives Autopilot's own skill — told to read the `SKILL.md` at an
exact path — which is a mechanism this runtime exercises on every turn. If a
live measurement later shows multiple skill inputs work, attaching them is an
improvement, not a rewrite.

### The skills she already has

A hiring layer that cannot see the skills the user installed herself will
report a capability she already has as unmet, or fetch a second copy of
something on her own disk. Autopilot therefore reads
`$CODEX_HOME/skills/<name>/SKILL.md` — **read-only, always**, because nothing
Autopilot does writes into her Codex home, and reading hers must not become
the exception that reopens that. Codex's own preinstalled `.system` skills are
skipped: they are available to every session already.

The screener sees them as `skills_on_this_machine` and is told to prefer them:
using one costs nothing, fetches nothing, and it is her own choice of tool
rather than outside material. When a capability is filled that way, the record
says so — `the market was not consulted for this capability` — so the reason is
on file rather than inferred later. Her skill is used **where it is**; nothing
is copied into the project.

An entry in that directory that is not a skill is named and skipped, not fatal.
Her skills directory is a general-purpose directory this runtime merely
observes; one unusable folder in it is not a reason to stop her run. That is
deliberately the opposite of the rule for `.codex-autopilot/skills`, which is
configuration somebody wrote *for* Autopilot, where a broken file stops loudly.

### Why "local" is not a loophole

An installed skill is not withheld from the verifier the way a market pack is.
That is the one place a hostile reader should push, because "call it local and
the withholding goes away" is exactly the shape a bypass would take. It cannot
be taken:

* **Provenance comes from where the runtime read it, never from anything
  claimed.** A bundle is local because *this code* found it in her Codex home.
  The requisition has no field that can assert provenance; `origin`,
  `provenance`, `local`, `source` and `trusted` are refused outright as unknown
  fields, and a test drives each one.
* **An installed item is a name, not a path.** `installed: "taste"` is one
  directory component, checked against a regex before it is joined to anything,
  and resolved against the runtime's own listing. A screener cannot point it at
  a bundle it staged.
* **The two cannot be combined.** An item offering both `installed` and
  `bundle` is refused: one capability yields one outcome, and "an installed
  skill that is also a fetched bundle" is the bypass written out.
* **The record must agree with itself.** A `local` bundle carries no provider
  and a `market` one must have one, because the trust ladder refuses external
  evidence without a provider. A record that disagrees is refused on read.

The substantive reason, not the mechanical one: R18 governs content that
arrives through a channel a model can influence. A skill in her Codex home
arrived because she put it there. It is closer to `user_instruction` on the
trust ladder than to `external_text` — still unverified, so it still cannot
make a Skill Pack trusted and the qualification gate is untouched, but it is
her standing instruction, and her instruction legitimately shapes both the
worker and the acceptor.

**The residual risk, named rather than hidden.** A worker could run Codex's own
`skill-installer` during a run and cause a skill to appear in her Codex home;
Autopilot would then read it as local. Autopilot cannot prevent that, because
it is her Codex running under her permission profile, and it never sees that
install happen. If this needs closing, the shape is an inventory snapshot taken
at run start and pinned by digest, so anything appearing mid-run is external by
construction. It is not built, and it is the owner's call whether it is worth
the cost of a skill she installs mid-run being unusable until the next one.

### Two directories, not one

They are easy to merge by accident, so plainly:

| path | what it holds | who writes it |
| --- | --- | --- |
| `.codex-autopilot/skills/` | Skill **Pack manifests** — one JSON file per revision, the procedures/checks/evidence-roles record the resolver reads | the user; Autopilot only reads |
| `.codex-autopilot/hired-skills/` | Skill **bundles** fetched from a repository | Autopilot, on admission; revoked by a named command |
| `$CODEX_HOME/skills/` | Skill bundles the **user installed herself** | the user; Autopilot only reads, never writes |

A pack manifest describes and governs. A bundle is the skill itself.

### What the installer may not reach, structurally

The admission path resolves every destination under
`<project>/.codex-autopilot/hired-skills` and refuses anything else. It cannot
write to the Codex plugin cache, `$CODEX_HOME/skills`, hooks or MCP
configuration, because it cannot name a path outside the project at all — and
a test drives it at each of those paths and requires a refusal. That is the
structural form of the rule, not a sentence in prose asking for good
behaviour.

### The argv rule is unchanged by the source

A skill arriving from GitHub does not widen what may be executed. It brings
procedures, checklists, failure modes and quality criteria, and proves itself
against checks the project already ran. R18 is unchanged too: it shapes HOW
the work is done and never decides WHETHER it is accepted, which is why such a
pack is still withheld from the verification-phase prompt.

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
