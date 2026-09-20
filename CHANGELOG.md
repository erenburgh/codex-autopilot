# Changelog

## 0.11.6-beta

The traces stop piling up. 0.11.4 disclosed that nothing removed them; this
removes them.

Each dispatcher sweeps `.codex-autopilot/logs/` before it opens its own trace:
whole files, oldest first, until the directory fits `runtime.log_retention_mb`
(512 MB by default; 0 keeps everything). Replayed against the pile that was
actually measured - 167 files, 2338 MB - it leaves 504 MB in 36 files and frees
1834 MB.

What holds it back, each tested rather than trusted:

- **Nothing is truncated.** A capped trace loses its tail, and the tail is where
  the failure is - the on-call engineer reads these to find out what the server
  actually said. Whole files go, or nothing does.
- **The newest five survive** whatever the budget says. A ticket opened an hour
  later must still find the traces it is about.
- **Nothing written in the last hour is touched.** Deciding whether another
  process holds a file handle is not portable; age is, and a live dispatcher's
  own trace is by definition fresh.
- **Only this runtime's own files are eligible**, matched by the `app-server-`
  and `automatic-relay-` prefixes. Anything else in that directory belongs to
  whoever put it there.

`staged-artifacts/` is deliberately not swept: those are a task's working copy
of the project, not a diagnostic, and deciding when a copy is finished with is
not this sweep's business. The footprint document says so and says removing them
is the owner's `rm -rf`.

## 0.11.5-beta

`git init` was never enough, and every document said it was.

The Git check asked one question: does `.git` exist. A repository that has been
initialised and never committed passes it, and then fails where it matters.
`observe_changed_paths` compares against `HEAD`; without a commit there is no
`HEAD`, `git diff` refuses, and `artifact_staging_lifecycle` records
`scope_not_observed` and carries on. The run works. R7 - the rule that catches a
worker writing outside the scope it declared - is simply not enforced, for every
task, and nothing tells the user. `git init` with no commit is exactly the state
a first-time user is most likely to be in, and the requirement line, the
refusal message and Getting Started all advised precisely that and stopped.

- Preflight now reports `Git: WARN` when the repository has no commits, says
  that the declared write scope cannot be observed and will be recorded as
  unchecked, and says that one commit enables it. It is a warning, not a
  refusal: the run is still legitimate, it is just less guarded than the user
  would assume.
- Both refusals - `bootstrap` and `preflight` - now name the commit rather than
  only the repository.
- The README and Getting Started say what git is actually for here, because the
  requirement reads as bureaucracy until you know: it is local, unrelated to
  GitHub, needs no remote, and Autopilot creates no commits of its own. It is
  how the runtime sees what a task changed - `git diff --name-only` plus
  `git ls-files --others` - and that observation is the whole basis of the
  write-scope audit.
- The README's requirement line also stopped listing Python and the Codex CLI as
  things to arrange: `install.sh --install-deps` installs both when they are
  missing.

## 0.11.4-beta

`logs/` is what fills the disk, and 0.11.3 said it was `staged-artifacts/`.

0.11.3 disclosed the staged workspaces and called them "the largest thing
Autopilot puts in a project". That was written from the code without measuring,
and it is wrong by two orders of magnitude. Measured on the author's own run:

```text
2.3G  logs
 28M  staged-artifacts
6.3M  run-state.json
```

The project directory was 2.4 GB and 2.3 of them were App Server wire traces -
167 files, one per dispatcher, each holding the whole conversation in both
directions, individually between 70 and 108 MB. Nothing in the runtime rotates,
truncates or deletes them; there is no cleanup path for `logs/` anywhere.

Both the footprint document and the README now say this, with the numbers, and
say the thing a reader needs next: these are debugging traces, not the run's
memory. `run-state.json`, `plan.json`, `memory.sqlite3`, `memory-backups/` and
`handoff/` together are a few megabytes, and deleting old
`app-server-dispatcher-*.jsonl` between runs is safe - keeping the newest few
while an incident is open, because that is where the on-call engineer reads what
the server actually said.

Rotation itself is not in this release: it changes behaviour rather than a
sentence, and the disclosure was the part that was owed immediately.

## 0.11.3-beta

Six more places where a document said something the code does not do. Found by
reading README, Getting Started, every config key and command, and the two
safety documents against the source - not by using the product.

### What a first run actually asks of you

- The README described the memory permission as a Codex dialog where you "choose
  `Always`", one line under a promise that nothing needs a terminal. There is no
  dialog: the request goes to the dispatcher's own connection, which never
  answers approvals, so nothing pops up. Preflight stops before any worker and
  prints one terminal command ending in `--approve-project-memory-always`, and
  running it yourself is the consent. Getting Started and both skills described
  this correctly; the README did not.
- `Resume Codex Autopilot.` is the one control that is deliberately **not**
  answered by the hook. Blocking it would leave the turn interrupted and start
  nothing - that turn's own Stop event is what launches the dispatcher. The
  README stated the "every control is a blocked message" rule with no exception,
  so the one reply that proves resume worked looked like the one that proves it
  did not.

### What is on your disk

- `staged-artifacts/` was in no document. A task that declares a filesystem
  deliverable is run against a **full copy of the project tree**, once per such
  task, skipping only `.git`, the state directory and the usual caches. Nothing
  ever deletes those copies - there is no `rmtree` in `artifact_staging.py` - so
  a graph with several isolated tasks grows the project directory by several
  times the size of the repository, and `--purge-project-state` moves that
  weight aside rather than freeing it. Now in the footprint, with the fact that
  deleting a finished run's copies is safe and yours to do.
- `PROJECT_STATE.md` and `DECISIONS.md` were listed beside `ROADMAP.md` with no
  path, among entries that all carried one. Only `ROADMAP.md` is in the project
  root; the other two are written into `.codex-autopilot/`.

### Two counts that were simply wrong

- The memory MCP tool's operation union is 17 branches, not 16:
  `store_department_rubric` - a write operation - was missing from the only
  document that enumerates the API a reader inspects before granting `Always`.
- `desktop_notifications` raises a banner on four events, not three. The fourth,
  `_notify_start`, fires on every launch, so on a twenty-five-task run the key
  costs twenty-five start banners the document never mentioned. Getting Started
  had it right and the runtime document disagreed with it.

## 0.11.2-beta

The capacity line stops asking a question it cannot ask, and starts naming the
number that is actually running.

- The skill told the model to show the user the `Capacity:` line and "let them
  answer before the first worker starts". No model could obey it: preflight
  prints that line from inside `start-skill`, the same command that creates the
  run and Worker 1, so by the time anyone reads it the number is already in the
  plan. The instruction now says what it is - a disclosure, shown verbatim in
  the first reply after the command returns, with how to change the number
  (name another and repeat the start with `--replace`) and a standing rule
  never to present the template's ten as the user's own choice.
- The line itself was worse than unfollowable on Plus. `default_workers`
  recommends three there, is called by this notice and by nothing else, and
  narrows no plan: the one person warned that their window is narrow was told
  "3 parallel workers by default" while ten was what ran. The notice now names
  the run's real number and keeps the recommendation beside it.
- "You can change it at any time by naming another number" was not true either.
  Nothing changes the worker ceiling mid-run; the text says so.

## 0.11.1-beta

What a stranger receives, audited as a stranger receives it: the published
0.11.0-beta archive was downloaded from the release, unpacked, and read against
its own documents. Everything below is a place where the build would have
misled someone who had never seen it before.

### The one grant, disclosed and taken back

- `install.sh` writes a marked block into the Codex execpolicy
  (`$CODEX_HOME/rules/default.rules`) allowing its own installed script to be
  launched with `start-skill` and `timeline` - without it Codex raises an
  approval dialog, the dispatcher answers no approval by rule, and the run
  hangs in a task nobody is looking at. That block is written outside
  Autopilot's own directory, and three shipped documents said installation adds
  nothing there but the launch agent and changes no Codex approval settings. It
  is now named in README, in Getting Started's list of what the installer does,
  and in the footprint document, with what it allows and what it does not.
- `uninstall --yes` now removes it. It used to leave two standing
  `decision="allow"` rules pointing at a script it had just deleted. A rules
  file you or Codex also wrote into is kept without the block; one that held
  nothing else is removed.
- The marker and the rule text moved to `src/codex_autopilot/execpolicy.py`, so
  the writer and the remover cannot drift apart.

### Numbers that said the opposite of the code

- The screening line counted an installed bundle as an unfilled need.
  `HiringDecision.unfilled` excludes `installed` deliberately - the worker did
  get that skill - but the status card compared against the single string
  `hired`, so a successful hire rendered as "0 skills hired, 1 need unfilled".
- The task-graph table and the scheduler document gave `serial` and `1` as the
  schema defaults. They are the fallback for a project with no `[runtime]`
  section; a fresh run is `auto` with ten worker slots.
- Two documents said the suite is 1019 tests. It is 1194.

### What 0.11.0's own documents got wrong about hiring

These were written a day earlier, in this repository, and read against the code
for the first time here.

- The `status` card in chat did not carry the screening line at all - only the
  detailed report and the CLI did, while three documents said `status` reports
  what hiring spent. The card now carries it whenever screening is on, which is
  the case where it costs something.
- The docs said the screener sees the bundles this project hired earlier. It
  does not: the brief holds the skills installed in your Codex home and the
  packs the plan and the project's pack library declare. A bundle hired on an
  earlier run is handed to the worker that hired it and is never offered back,
  so a later task can ask for the same capability again. The documents now say
  so, and closing that loop is named as open work.
- `auto` was described as screening "only when something is available to hire".
  It looks at the plan and the project's pack library, and not at your
  installed skills.
- The 512 KiB fetch ceiling was listed among the configurable bounds. Only the
  hosts and the timeout have settings.
- `docs/SKILL_SCREENING.md` documented a requisition item with `draft` and
  `sources` fields that the parser refuses outright. That design was considered
  and set aside; the section now says so and names what shipped instead.
- `docs/RATE_LIMITS.md` promised a retry budget of 96 attempts. It is 5 per
  failure signature, after which a ticket is opened for the on-call engineer.
- Getting Started said `--help` lists exactly eleven hand-typed commands.
  `skills` and `revoke-skill` made it thirteen.
- `docs/TESTING.md` gave the suite command with a bare `python3`. The runtime
  imports `tomllib` and needs 3.11+; the `python3` on a stock macOS is 3.9.

### Commands a reader could not run

- README's shell blocks called a bare `codex-autopilot`, which the installer
  deliberately never puts on PATH, so every copy-paste answered
  `command not found`. They now use the same full launcher path Getting Started
  uses, and say why.
- Both profile skills carried a sentence that had lost its beginning: an edit
  replaced the clause in front of "execution or the plan was migrated from
  v0.8" and left the tail behind. The planner read a fragment.

## 0.11.0-beta

Hiring. A task is screened for the skills its worker needs at the moment that
worker is hired, not when the plan is written.

### The screening session

- Before a task is reserved, a short screening session reads the task, the
  skills installed on this machine (`$CODEX_HOME/skills`, read-only), and the
  bundles this project hired earlier, and decides what the worker for that task
  carries. Nothing about skills is declared in the plan: a plan is authored
  before anyone has looked at the repository, so it cannot know.
- A screening is a session like any other - it is reserved, it is owned, it is
  bounded. Two attempts per task per graph version; a task whose screening
  cannot conclude proceeds unscreened rather than waiting forever, and the
  status card says so.
- A decision is `hired` only when a skill is actually attached to it. The flag
  is derived, not stored, so a record cannot claim a hire it does not hold.

### The market

- A screening may name a skill it does not have, as a provider
  (`github.com/<owner>/<repo>`) and a path inside it. The runtime performs the
  fetch; the screening session is told it must not reach the network itself.
- Only `github.com` and `raw.githubusercontent.com` are allowed by default,
  through `runtime.skill_fetch_hosts`, and the timeout through
  `runtime.skill_fetch_timeout_seconds` (20 seconds). The size ceiling - one
  `SKILL.md` up to 512 KiB - is fixed and has no setting. An empty host list
  means nothing is fetched at all.
- A fetch that fails is an unmet need on the record, not a task that stops.
- Only Skills are ever installed. Plugins, hooks and MCP servers are not, and a
  fetched bundle is copied into the project's own
  `.codex-autopilot/hired-skills/`; the destination is confined structurally,
  so a locator cannot write anywhere else. `~/.codex/skills` is read and never
  written.

### What the user sees

- `status` gained a screening line: the mode, the threads it has spent, tasks
  screened and unscreened, skills hired, needs left unfilled, bundles installed.
  A feature that is on by default has to be able to say what it cost.
- `codex-autopilot skills --project <path>` lists what a project has hired;
  `codex-autopilot revoke-skill --project <path> --skill-id <id>` removes one.
- `runtime.skill_screening` is `always` by default; `auto` screens only when
  something is available to hire, `never` switches it off.

### Verified

- The deterministic suite is 1187 tests and ends `OK`.
- Live: a screening session ran inside a real run on a real project, reached
  COMPLETED, and recorded its decision. That decision hired nothing, which is a
  verdict and not an omission. A live hire and a live fetch have not happened
  yet in a real run; both are covered by tests only.

## 0.10.0-beta

The first release meant for people other than the author. Everything in it
was found on a live run of the previous version: every entry below is a
place where the run stood still and a human had to step in.

### DevOps repairs the runtime itself

- The on-call engineer may now change the runtime's own code. The repair is
  not declared, it is proven: a reproduction test must fail on the current
  code and pass with the patch, the whole suite must stay green, and the
  guarded ownership, trust and classification definitions must stay
  byte-identical. Anything else and the installation is untouched. A repair
  is a set of edits — several modules, a new module — applied together;
  half a set never reaches the installation.
- The engineer's authority moved into `engineer_authority.py`, which no
  repair can touch; `pipeline_engineer.py` itself became repairable, with
  five of its definitions guarded by hash.
- The installer ships the test suite next to the sources: without it a
  repair cannot be proven, and self-repair would silently disappear on a
  user's machine while staying green in the repository.
- Recovery actions are named from a vocabulary, never described in prose.
  On the previous run the main failure signature had 18 repeats, 15
  recorded resolutions and zero learned runbooks, because the learning
  path compared free text against an enumeration. `--action` is now
  mandatory on `devops-resolve-incident`; circumstances go to `--note`.
- A repeated failure is bounded per signature (R23). `maximum_attempts`
  used to be declared and read by nobody; it is now the ceiling per
  failure signature, default 5, and reaching it opens a ticket for the
  engineer rather than stopping the run.

### The run no longer waits for a human word

- A retry due after a rate limit is raised by the runtime itself. The last
  dispatcher and the Stop hook leave a wake-up process behind; it sleeps
  until the due time and dispatches the same way an automatic successor
  would, under the same owner and the same ownership check.
- The wake-up survives a reboot: the installer adds a launchd agent that
  sweeps known projects at login and every five minutes and arms a wake
  where a retry waits. The owner is derived from the run's own journal.

### The user hears what they need before the first worker

- Both skill profiles carry an onboarding block: the two decisions that
  belong to Codex, how to look at the run, what a running task means and
  what may be done with a finished one, how the plan changes, what happens
  on a fault and who repairs it. Every phrase the onboarding promises is
  one the hook knows — that is tested.
- `tasks` / `задачи` answers with the same card as `status`; bare `stop`,
  `pause`, `resume`, `continue` and their Russian forms are accepted like
  bare `status`. Uninstalling still requires the full product name.
- The engineer writes everything a person will read in the run language,
  as the workers already did.

### The harness speaks English

- Rules, the launch ladder, hook replies, preflight, CLI output, failure
  messages and notifications are English. Worker and engineer prompts were
  already in the run language and are untouched. The short status card —
  the one thing read in chat — follows the run language. The owner's
  quotes in the rules' `source` fields stay verbatim.
- A long-standing concatenation bug glued words together inside the rules
  that reach every prompt; fixed.

### The repair path was broken where nobody looked

- `devops-rearm-relay-owner` — the command the on-call engineer uses to
  restart a stalled run — could not run at all. It reached for a name the
  lifecycle facade does not re-export and died with `AttributeError` before
  doing anything. Its tail function had a test; the command itself was
  imported by the test suite and never called once. It is now executed by a
  test that drives a real known-failed create, and only the process spawn
  and the fifteen-second launch observation are substituted.
- The same command closed its incident without naming the repair, which
  this line's own gate refuses — so it failed in the one phase it exists
  for: the ticket still held by the engineer.
- A reinstall of the same version deleted every accepted runtime repair.
  The gateway writes its patches and the repaired sources inside the
  installed version directory, and the installer began by removing it while
  the archive loop skipped the current version by name. Measured: patches
  and repaired sources gone, the backup directory empty. A repaired
  installation is now moved aside as `<version>.repaired-<stamp>` and the
  path is printed; nothing is deleted (R28).

### External content still does not decide (R18)

- Conflict resolution was the only writer of the `verified` transition for
  a Truth, and it re-checked nothing. Three calls of the single exposed
  memory tool — attach a contradiction, attach support while the record is
  disputed, resolve — turned unverified outside material into binding
  support for a verified fact. Measured on a live database. The same line
  also hardcoded `verified` for every outcome but supersession, so a
  retired Truth came back to life.
- An acceptance cited every evidence item of its milestone, and the trust
  gate refuses the whole set when one item is below deterministic. Since
  the memory tool refuses evidence without a milestone link and its own
  description requires outside material to be labelled `external`, a worker
  that obeyed the tool made its own task impossible to accept — and the
  refusal escaped after the Stop hook had already fired, so the turn was
  lost. The acceptance now rests only on what may support it; the outside
  material stays on the record.

### One file is one lock

- Filesystem claims were compared by exact string while named claims were
  deliberately case-folded. On the filesystem Codex Desktop runs on,
  `Shared.json` and `shared.json` are one file — and the coordinator handed
  an exclusive write lock on it to two tasks at once.

### Hook trust is checked on every path that starts work

- Two repair commands and the Stop hook's continuation started production
  work with the trust gate checked by nobody in any process on their path.
  A relay is executed by the Stop hook, so arming one while the hook is not
  trusted promises a launch that cannot happen; all three now refuse.
- The test substitution for that gate had the same class of defect inside
  itself: it matched a substring and therefore saw only modules importing
  the gate alone, leaving others talking to a real App Server. It parses
  now.

### Smaller, each measured

- The wake-up kept a narrower copy of "the owner's turn is over" and
  accepted only one of the three proofs the dispatcher accepts. For a run
  whose owner ended on an interrupt it found no owner at all: after a
  reboot the sweep skipped the project and the retry waited for a human.
- The replanner — the one phase that rewrites the whole graph — was asked
  to report the rule ids it applied while its prompt carried no rules at
  all.
- The milestone gate refused a missing link and accepted any non-empty
  string, including the very label the measured worker used instead of a
  milestone id. A name that belongs to no task is refused now.

### From the v1.0 line

This release carries the nine fixes made on the main line the same week: a
run-state ceiling that follows the budget instead of crashing the
dispatcher past the hook, a DevOps re-arm gate that tells "in progress"
from "broken" instead of cancelling every repair, one calculation for the
worker limit instead of three, one predicate for "the turn is over"
instead of two, state set aside instead of destroyed, and four places
where the code said something other than what it did.

### Measured on the previous run

- Every Pipeline Engineer prompt line names every flag its command
  requires — `relay-fail` had gained a required `--failure-code` while the
  runbook still showed the old invocation, and an engineer following it
  would have been refused by argparse. Tested as a class, not a case.
- The status card no longer claims the dispatcher is dead during a
  verification, and no longer prints a model and reasoning that nothing
  wrote.

## 0.9.1-beta

Fixes found on the live 0.9.0 run. Every one of them was discovered not by
reading code but by the run standing still for the user: each entry below is
a stop that had to be worked out from the journals.

### The launch no longer hangs silently

- The trust probe ran at the production worker's effort. A turn whose whole
  job is one harmless tool call was counted at `xhigh` and twice missed the
  five-minute mark. The probe now has its own effort.
- `Timed out waiting for App Server` with a healthy App Server sent people
  to repair transport and permissions. Waiting for a turn raises
  `TurnTimeout`, which says plainly that the model turn ran over.
- Five minutes of silence got a voice: before the probe it prints what is
  being checked, in which task and how long is allowed. One timeout no
  longer fails the launch — there are three attempts.

### Worker and acceptance

- The worker did not know that a command requiring permission kills the run:
  the dispatcher never answers approvals, and the dialog hangs in a task
  nobody is looking at. It now finishes with `BLOCKED DANGEROUS_PERMISSION`
  and names the command.
- An acceptance refusal exhausted the revision attempts and put the task in
  BLOCKED — with no replanner, no engineer and no command to lift the state.
  The task is now re-hired at the next effort step with a fresh executor; the
  plan and the Definition of Done are untouchable.
- The on-call engineer, having closed an incident, assigned no successor: the
  run went to READY/PREPARING and stood silently. The engineer's own turn
  became the causal link.

### Context budget

- The original request was copied whole into every prompt. On a run with a
  detailed specification that is 51 475 characters out of 62 635 under a
  64 000 ceiling — not one task would have assembled. It is now a reference
  with a length and sha256, and the text is fetched from Project Memory.
- The 64 000 ceiling had no justification against a model window of
  258 400 tokens. Derived from the window and recorded in
  `docs/CONTEXT_BENCHMARK.md`. A second copy of the same constant in the
  planner prompt was killing the relay in the middle of a run.

### Installation and hooks

- Hook trust was lost before every new task. Codex clamped the declared
  `Interrupt` timeout to its own limit and thereby rewrote our definition on
  every load; the unit of trust is the whole file, so all three hooks went
  back to review.
- The run stored the skill path together with the version number. The first
  install left the reference dangling and killed the active run — that is,
  the product could not be upgraded at all. The path now goes through the
  stable `current`, and runs of earlier versions are healed on load.
- A new run inherited the previous run's open tickets and stood on them
  before its first task.

### Failures stopped hiding the cause

- A failure before the request was sent counted as ambiguous:
  `installed_plugin_root` was computed among the call arguments, i.e. after
  the "request went out" flag. Such a failure opened an
  `AMBIGUOUS_SIDE_EFFECT` ticket, where both auto-repair and the engineer are
  forbidden.
- A repeated failure of a task already waiting raised
  `IllegalTaskTransition: RETRY_WAIT -> RETRY_WAIT`, killed the relay and
  opened a second ticket on top of the first — the real cause ended up
  hidden under the consequence.
- `NameError` instead of a clear refusal: the exception was not imported in
  the module that raises it. A test now holds this class of error for the
  whole runtime.

### The user's answer to an escalation

- Resuming closed escalations only in one phase of the run, and that phase
  is set solely by the engineer's completion. A ticket escalated by routing
  waited for a human, the human answered — and the answer was lost.

611 tests.

## 0.8.2-beta

Continuation of the 0.8.1 revision and the first really working on-call
engineer lane. Acceptance on live Codex Desktop passed: three milestones,
six workers, every task inside the project, zero tickets, DONE.

### The on-call engineer became a worker

- `ensure_pipeline_engineer` changed a field in JSON and called that an
  engineer. Now an incident in the `PIPELINE_ENGINEER` class reserves a real
  session of kind `pipeline_engineer` — ahead of all other work and without a
  single resource, because what it repairs is the very queue it stands in.
- `build_pipeline_engineer_prompt` had been removed in 0.8.1 as unused.
  There were no references to it not because it was replaced but because the
  lane was never finished. Restored and rewritten: it names real commands
  (`relay-status`, `relay-complete`, `relay-fail --definitive`,
  `devops-rearm-relay-owner`, `arm`, `devops-resolve-incident`) instead of
  invented ones.
- R13 in the prompt text: the engineer has full authority to repair, it
  chooses the method, the user takes no part in the choice. Escalation is
  exceptional and requires a code from the closed list
  (`DANGEROUS_PERMISSION`, `GLOBAL_CONFIG_CHANGE`, `PROJECT_DAMAGE_RISK`,
  `RECOVERY_EXHAUSTED`, `PRODUCT_DECISION`, `ARCHITECTURE_DECISION`); a bare
  `ESCALATE_TO_USER` is no longer accepted.
- The thread digest (`server_view`) is gathered by the dispatcher and placed
  in the incident package. Before, the engineer would have had to request
  permissions for commands it does not have; now there is nothing to ask —
  everything is already in the package. The prompt is rebuilt at the start
  of the turn, not at reservation, so the picture is fresh.
- The new command `devops-resolve-incident` closes the ticket: it requires a
  healthcheck name and observations, and `RESOLVED` happens only if the
  ticket is really closed.

### Fixed

- A plan change no longer requires a verbatim echo of `user_request`. In the
  live run that is 35 234 characters: a model rewriting the graph does not
  reproduce such a string, so **no** plan change could pass. The field is
  carried over from the current plan — stricter than an echo, which could be
  forged. `goal` and `model_strategy` stay strict.
- `turn/start` runs only on a thread loaded by this connection: if the thread
  is not in `subscribed_thread_ids`, it is first raised through
  `thread/resume`.
- A broken `dispatcher_pid` in the state is a refusal, not a guess. Before, a
  non-numeric value was silently read as "the process is alive".
- The plan template in both skills carried `execution_strategy="serial"` and
  one worker. `plan.py` declares `auto` and two as the default, but the plan
  is written by the planner from the example in `SKILL.md` — and an explicit
  value in the file cannot be overridden by a default. Not one new run
  entered parallelism. It is also stated what the default does not give:
  parallelism is created by the shape of the graph, and siblings writing to
  one file are serialized by the resource lock.
- The creation causality audit (R1) is called from the status report. The
  functions were written and called only from tests: the claim "the chain is
  checked" was not backed by a call path. The first run on live state showed
  two breaks nobody had seen.

### Removed

- Five definitions hidden by the `lifecycle` facade, two arguments with a
  single allowed value, 50 unused imports in the memory modules. The facade
  now re-exports exactly what is imported through it.

### Closed from the independent review set

- **R6.** `ensure_project_root` silently called `project/update` on every
  task creation and appended the canonical root to the user's saved
  project. Reading and writing are now separated: writing requires an
  accepted user decision naming this project and this root
  (`authorize-project-root --yes`, lifted by `--revoke`). Membership is
  fixed too — it is determined by nesting, the same rule by which preflight
  picks the project.
- **R13.** A worker's `BLOCKED` and `ESCALATE` carry a code from the closed
  list. Before, the reason went into `last_error` as the string "M9 worker
  returned BLOCKED", which holds nothing beyond the status itself.
- **R5.** A measured placement discrepancy fails the launch verdict and goes
  into one normalized ticket; before, `visible_in_desktop` was not among the
  deciding items at all, and `OUTSIDE` changed nothing. An unmeasured state
  stopped being eternal: 180 seconds after the thread's creation it becomes
  a negative result.
- **PRE-SIDE-EFFECT-FENCE.** A retired Desktop task refuses on
  `UserPromptSubmit`, before a single model or tool call. Before, the closed
  refusal came at the end of the turn, i.e. after the work.
- **R18.** Provenance of external material is mandatory on intake; the
  `external` label appeared in the memory tool schema; a Constraint on
  external material, the transition into a binding state and appending
  external support after the fact — closed. `contradicts` stays open.
- **R1.** The causality audit is called from the status report.
- **ENTRYPOINT-DEFAULTS.** The plan template enters the declared default.

### Known and not closed

At that point three items of the independent review were open: per-task
accounting of the shared tree with time, attempt and token budgets (R7); the
phase contract for the verifier, the replanner and the engineer (R16/R17);
and discipline leads with a versioned rubric (R30). Recovery stayed partial: the
list of allowed actions was still prompt text, and the real boundary was the
set of guarded commands.

## 0.9.0-beta

The first version in which the 0.8.0 audit is closed entirely, and the first
whose promises are checked against the code by tests.

### The 0.8.0 audit is closed

1. **The dead pipeline is removed.** `orchestrator.py`, `smoke.py` and the
   `headless_app_server` surface — about 1165 lines no run could execute.
2. **The on-call engineer got a body.** The skill promised a role whose code
   did not exist. Now an incident in the `PIPELINE_ENGINEER` class creates a
   real visible worker; it repairs on the user's behalf, and escalation
   requires a code from the closed list.
3. **The pre-created slots mechanism is removed entirely.** It worked around
   a supposed impossibility of creating a visible task through App Server;
   the premise was refuted by a live run.
4. **The empty project binding is removed** from the creation path.
5. **CLI commands sorted out.** Each has a named consumer: user commands are
   described in the documentation, repair commands in the engineer's tools,
   internal ones carry their own help. The rest are removed.
6. **Memory refusals name what is accepted.** Before, a worker tried values
   blindly and went off to read the plugin's sources.

### Checked live

Two full runs on a real project: parallel workers on one dependency
boundary, unblocking, independent verification, an incident with a coded
escalation and the user's answer, canonical placement of every created task.
Installation from the release archive followed by a `doctor` check.

### Left unchecked

External installation by anyone but the author; a CI run on the declared
Python 3.11; the `--install-deps` path on a machine without Python and Codex
CLI; multi-hour recovery after a rate limit; Computer Use unattended;
Windows. All of this is named in the README, not hidden.

- Added independent contract regressions for the required v0.9 execution
  default, Desktop-owned start surface, exact thread-title formats, canonical
  project association, and deterministic verification promotion.
- Added separate deterministic AI Studio acceptance shapes for independent
  implementation branches plus integration, a research/analysis/fact-check
  pipeline, and mixed code/Computer Use scheduling.
- Added the required dependency-graph, parallel-execution, roles,
  resource-locks, thread-naming, project-association, and testing documents.
- Recorded release-blocking candidate gaps in the author's working notes,
  which stay in the repository and are not part of the release; no release,
  tag, push, or publish was performed.

## 0.8.1-beta

A revision after the first successful 0.8.0 acceptance: what could not
execute was removed, and what promised the impossible was fixed.

### Removed as unreachable

- `orchestrator.py` (1130 lines) and the `headless_app_server` surface.
  `run`, `resume` and `_dispatch` refused under `desktop_owned`, and the
  default of every command was exactly `desktop_owned`. The only live entry
  points were `smoke.py` and the tests. With them went `smoke.py`, the
  commands `run`, `_dispatch`, `test desktop`, `restore-app-server` and
  `restore_app_server_transport`.
- The pre-created slots mechanism: `add-worker-slot`, `append_worker_slot`,
  `worker_thread_ids`, `worker_slot_cursor`, `_validate_worker_slots`, the
  `WAITING_PROJECT_SLOT*` phases. It was a workaround for a supposed
  impossibility of creating a visible task through App Server; the premise
  was refuted — all six 0.8.0 acceptance workers turned out inside the
  project.
- Re-binding a thread to the project after creation. The line above rejects
  creation if the thread is not in the right project, so
  `thread/metadata/update` was binding the already bound. v0.7 does not call
  it.
- The `threadSource` parameter in `start_thread`: the value
  `agent_created_thread` marked the task as created by another application.
- Eight functions mentioned nowhere: `system_roles`,
  `build_pipeline_engineer_prompt`, `send_message_payload`, `_transport`,
  `_require_transport_claim`, `_healthcheck_passed`, `_sha256`,
  `_validate_owner_against_state`.

### Fixed

- The Pipeline Engineer lane got a procedure. Before, the skill promised that
  DevOps would repair and re-arm, and there was no code creating the engineer
  at all: `ensure_pipeline_engineer` changes a field in JSON. Now a sequence
  of existing guarded commands is named, and it is said separately that an
  unknown side effect remains a stop.
- The launch report is no longer presented as visible. The Stop hook must
  answer `continue`, otherwise the initiating turn stays `interrupted` and
  the dispatcher does not start; so the report is not shown. The initiating
  turn must name the phrase `status`, which goes through `UserPromptSubmit`
  and is visible.
- The memory MCP server's version was taken from a hard-coded string and
  would have diverged from the package on any version bump.
- The default `worker_surface` in the config was `headless_app_server`: a
  new run got a non-working surface unless one was chosen explicitly.

### Coverage

- `test_model_routing.py` — model routing directly, without the dead
  orchestrator: the routing table and the absence of a silent model swap.
- `test_skill_promises.py` — the skill may not promise what the runtime does
  not do; it also checks that the procedure names no non-existent commands.
- Removed 24 tests of the dead path, `test_recovery.py` and
  `test_context_budget.py` entirely: the latter measured prompt growth with an
  assembly that no longer exists, while the live one has a hard
  `MAX_PROMPT_CHARS` limit.

## 0.8.0-beta

- Added clean-machine preflight for target root, Git, installed runtime, official App Server, `:workspace`, target cwd, built-in Project Memory MCP, SQLite FTS5, and Adaptive model metadata.
- Added an explicit code-77 approval path for official App Server access to its exact `CODEX_HOME`, with no project run-state created on failure.
- Added a short-lived per-user launch registry so an initiating task can safely start Autopilot for a different target repository after `turn/completed`.
- Added project-local evidence-backed Project Memory using SQLite/FTS5 and a bundled stdio MCP server bound to each worker's target cwd. The public MCP surface is one user-approved `memory` tool with 14 strict operations.
- Separated Truth, Decisions, Constraints, Questions, Observations, Evidence, and Conflicts. Truth requires validated non-migration evidence.
- Added bounded retrieval, stable IDs, pagination, audit history, milestone evidence gates, integrity checks, online backups, and recovery from the latest verified milestone backup.
- Made `PROJECT_STATE.md` and `DECISIONS.md` generated views; made `HANDOFF.md` advisory and capped at 8 KiB.
- Added conservative v0.7 migration with a complete backup and zero automatic promotion of old agent prose to Truth.
- Preserved serial visible worker rotation, deterministic Sol/Astra routing, Host Settings omission, rate-limit waiting, and approval fail-closed behavior.
- Added an explicit first-use Project Memory trust probe. The user chooses persistent `Always` trust in Codex; production code never answers that approval.

## 0.7.0-beta

- Added deterministic AUTO routing between GPT-5.6 Sol and GPT-6 Astra, explicit execution modes, model metadata validation, and AUTO-only capability escalation.

## 0.6.0-beta

- Introduced the model-neutral App Server core, trusted lifecycle hooks, serial visible workers, deterministic controls, installer, and clean release package.
