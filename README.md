# Codex Autopilot

Codex Autopilot turns large projects into a dependency-aware workflow of fresh
specialist workers. Independent work can run in parallel; dependent work waits
for verified prerequisites; important results can be checked by fresh
independent verifiers; and Project Memory carries evidence-backed context
without entire old sessions. The scheduler is deterministic code, not a
persistent manager LLM.

**Fresh workers prevent context decay; evidence-backed Project Memory prevents context corruption.**

Codex Autopilot is a Codex plugin plus a deterministic dependency scheduler. The
initiating task turns a goal into tasks. In the `desktop_owned` surface,
the scheduler atomically reserves only the READY frontier and records exact
causal provenance. The trusted Stop hook starts one local dispatcher under the
authorization the user already gave; that dispatcher performs persistent App
Server `thread/start` and production `turn/start` itself, waits for authoritative
completion, and exits. There is no separate detached launcher: a task is created
only by the exact causal predecessor that reserved it. The hook answers
`continue` on a healthy launch - a blocking answer would leave the initiating
turn `interrupted` and the dispatcher would never start - and blocks only to
show a failure. Codex App `create_thread` and `send_message_to_thread` are
not used, and no transition waits for a model continuation or new user input.
Infrastructure incidents are handled by the deterministic
`Pipeline Engineer · On call` capability described in
[docs/PIPELINE_ENGINEER.md](docs/PIPELINE_ENGINEER.md).

Desktop placement and canonical filesystem scope are distinct. The automatic dispatcher supplies the canonical cwd and App Server project ID, verifies returned title/cwd/project metadata, starts the production turn through App Server, and fully exits after the turn. App Server and Desktop project IDs are separate namespaces, so actual Desktop visibility/editability must be observed rather than inferred from App Server metadata. `thread/unsubscribe` is cleanup, not an ownership handoff. See [Desktop-owned runtime](docs/DESKTOP_RUNTIME.md).

Project Memory is a project-local SQLite/FTS5 database exposed to workers through a bundled stdio MCP server. Verified facts require explicit evidence; observations, decisions, constraints, questions, evidence, and conflicts remain separate record types. Each fresh worker receives a bounded set of IDs and constraints and retrieves details on demand instead of inheriting an expanding transcript.

The initiating request also establishes one durable BCP-47 response language for the run. Plans, reservation messages, worker prompts, commentary, and reports use that language; machine protocol tokens, code, identifiers, file names, and tool names remain unchanged. Existing configurations without a language keep the compatible `en` default.

## Install

Requirements: macOS, Codex Desktop, an eligible account, and a target project that is a Git repository - a bare `git init` is enough, no commit needed. Python 3.11+ and the Codex CLI are installed for you by `install.sh --install-deps` if they are missing.

Git here is local and has nothing to do with GitHub: no remote is needed, nothing is pushed, and Autopilot creates no commits (`git.auto_commit` is off). It is how the runtime sees what a task changed — `git diff` plus `git ls-files --others` — which is what the declared write-scope rule is checked against.

Open the Codex project you want to work on and say:

```text
Download and install this skill, then start working on this project with it:
https://github.com/erenburgh/codex-autopilot
```

Codex clones the repository and runs the installer itself. Nothing needs a
directory to be chosen: the project you are in is the project Autopilot works on,
because every task it creates is placed there. One terminal command may be needed
once, at first run, to grant the memory tool — see below.

Codex will ask for its own two trust decisions once - see below. They are Codex
security steps and Autopilot never answers them for you.

To install by hand instead:

```bash
git clone https://github.com/erenburgh/codex-autopilot.git
cd codex-autopilot
./install.sh --profile adaptive --install-deps
```

The macOS release ZIP can be extracted instead. `--install-deps` uses an existing Homebrew installation when Python is missing and npm when Codex CLI is missing. The installer never installs Homebrew itself.

The installer also leaves one background agent on the Mac: `~/Library/LaunchAgents/com.codex-autopilot.wake.plist`, loaded with `launchctl`. It runs `codex-autopilot _wake-sweep` at login and every 300 seconds for as long as it stays installed. A sweep arms the wake-up for a task whose rate-limit retry has come due, or for a started run that nobody is left to move (a ticket waiting for the on-call, ready work with no session, a reservation whose dispatcher died), and does nothing else - never for a run you paused; without it, a run asleep on a rate limit would wait for a human after a reboot, because the sleeping wake-up process does not survive one. `CODEX_AUTOPILOT_SKIP_LAUNCHD=1` at install time writes the agent without loading it, and `"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" uninstall --yes` boots it out and deletes it. The full footprint is in [Install and uninstall footprint](docs/INSTALL_FOOTPRINT.md).

After installation, start a fresh Codex task so the plugin loads. First use needs two permissions, and they are granted differently. The first is a real Codex dialog: open `/hooks` and trust the current Autopilot **Stop** hook. The second has no dialog at all. A dedicated preflight task calls the single local `memory` tool with `operation=current`; the request goes to the dispatcher's own connection, which never answers approvals, so nothing pops up. Preflight stops before any worker, names the diagnostic task, and prints one terminal command ending in `--approve-project-memory-always`. Running that command yourself is the consent — Autopilot neither grants, derives nor bypasses the permission, and it asks you in the conversation before it re-runs anything. Installed hook and MCP definitions use the permanent `current/bin/codex-autopilot` entrypoint, so reinstalling or refreshing the plugin does not change their command identity; another hook review is required only when the hook definition itself changes. Preflight verifies that the exact selected-plugin Stop hook is enabled, trusted (or managed), and points to that stable runtime before project initialization or Worker 1; modified or untrusted requires review, while missing, disabled, duplicated, or erroneous inventory fails closed. If Codex blocks the official App Server from its state directory, preflight names the exact `CODEX_HOME` path that needs one-time read/write approval, exits with code 77, and creates no project run-state.

## Use

Open a Codex project and ask Autopilot to work on it. The project's own directory is the target: every task Autopilot creates is placed in that project, so a directory that belongs to no Codex project cannot be a target. Autopilot says that before it starts, not in the middle.

```text
Use Codex Autopilot for /absolute/path/to/my-project.

Goal: Build the complete inventory system.
Break it into independently verifiable milestones and continue until DONE.
```

The Adaptive profile uses `auto` by default:

- GPT-6 Sol handles milestones verifiable with code, files, shell tools, logs, tests, and builds.
- GPT-6 Astra handles milestones whose Definition of Done requires real browser or desktop GUI interaction through Computer Use.
- Reasoning is independent: `medium`, `high`, `xhigh`, or `max`.

Explicit strategies are `Sol only` and `Astra only`. The Host Settings profile sends neither a model nor an effort field; App Server applies its current defaults to every fresh task.

Adaptive asks for those model ids exactly and never substitutes another, so `doctor` and preflight compare them against what your account is actually served: they say when a newer model of the same family has appeared, and if a pinned one is retired they name what is served instead rather than guessing. Moving to a new model stays a decision, not a surprise. See [model routing](docs/MODEL_ROUTING.md).

The no-model controls are:

- `status` - a few lines: progress, what is running, what blocks it.
- `status detail` - the full report: every task, its state, and the reason it is waiting.
- `Pause Codex Autopilot.`
- `Resume Codex Autopilot.` It is also the user's answer to an escalation: it
  closes an incident the Pipeline Engineer handed over and returns a task whose
  worker is no longer alive to retry.
- `Uninstall Codex Autopilot.` This one requires the full product name.
- `tasks` / `задачи` answers with the same card as `status`. The bare words
  `stop`, `pause`, `resume`, `continue` and their Russian forms are accepted
  like a bare `status`; the exact vocabulary is in
  `src/codex_autopilot/control_phrases.py`.

Every control except resume is answered by the hook itself, without a model turn.
Codex marks such an answer as a blocked message: that label means the hook
replied instead of the model, not that something failed.

`Resume Codex Autopilot.` is deliberately not blocked. That turn's own Stop
event is what binds the causal owner and launches the dispatcher, so blocking it
would leave the turn interrupted and start nothing. You get an ordinary model
reply carrying `Codex Autopilot resume is armed for this turn's Stop hook.` —
that, not the blocked-message label, is the sign that resume registered.

## Hiring

A task is screened before it is given a worker. A short screening session reads
the task, looks at the skills already installed in your Codex home and at the
governed packs this project's plan and pack library declare, and decides what
the worker for that task should carry. Nothing about skills is declared in the
plan: a plan is written before anyone has looked at the repository, so it
cannot know.

One thing it does not yet see: a bundle this project hired on an earlier run.
Those live in `.codex-autopilot/hired-skills/` and are handed to the worker
that hired them, but they are not offered back to the next screening, so a
later task may ask for the same capability again. Closing that loop is open
work.

A screening may also name a skill it does not have, as a public repository and a
path inside it. The runtime fetches that bundle - the screening session never
touches the network itself - and copies it into
`.codex-autopilot/hired-skills/` inside the project. Nothing is written to
`~/.codex/skills`, to the Codex plugin cache, to hooks, or to MCP configuration.
A fetch that fails is recorded as an unmet need, not an error that stops a task.

Screening is on by default. It costs one extra Codex thread per task out of
your limits, and the `status` card reports what it has spent and what it
bought. To turn it off, or to screen only when the plan or this project's pack
library declares a pack to hire from:

```toml
# .codex-autopilot/config.toml
[runtime]
skill_screening = "never"   # or "auto", or the default "always"
```

What this project has hired, and how to remove one bundle:

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" skills --project /absolute/path/to/my-project
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" revoke-skill --project /absolute/path/to/my-project --skill-id <id>
```

The executable is deliberately not on `PATH`; the full path above is how it is
run by hand.

See [skill screening](docs/SKILL_SCREENING.md).

## Persistent state

- `.codex-autopilot/plan.json`: canonical execution plan.
- `.codex-autopilot/run-state.json`: canonical orchestration journal.
- `.codex-autopilot/memory.sqlite3`: canonical project knowledge and evidence.
- `.codex-autopilot/memory-backups/latest.sqlite3`: last verified milestone backup.
- `ROADMAP.md` in the project root, and `.codex-autopilot/PROJECT_STATE.md` and `.codex-autopilot/DECISIONS.md`: human-readable views.
- `.codex-autopilot/MILESTONE.md`: current worker cache.
- `.codex-autopilot/logs/`: the full App Server wire conversation, one file per
  dispatcher. Before 0.11.6 nothing removed these and one real run reached
  2.3 GB across 167 files, against 28 MB of staged workspaces; the runtime now
  sweeps finished traces down to `runtime.log_retention_mb` (512 MB by default,
  0 keeps everything) before each dispatcher starts, never touching the newest
  five or anything written in the last hour. See
  [Install and uninstall footprint](docs/INSTALL_FOOTPRINT.md).
- `.codex-autopilot/HANDOFF.md`: short advisory note; never treated as evidence.

## Safety and current limits

Workers use the configured `:workspace` App Server permission profile. App Server preflight and automatic worker processes never answer approval requests; an approval request fails closed. Autopilot does not call Codex App task APIs, change Codex model, reasoning, sandbox or network settings, change Git configuration, or auto-commit by default. It writes exactly one standing grant: the installer adds a marked block to the Codex execpolicy (`$CODEX_HOME/rules/default.rules`) allowing its own installed script to be launched with `start-skill` and `timeline`, because the dispatcher never answers an approval dialog and a run would hang on one. Nothing else is allowed by it, and `uninstall --yes` takes the block back out. The memory server exposes one tool with a strict operation union; Project Memory has no network service, shell tool, raw SQL tool, embedding service, or external database.

The v0.11 beta supports macOS. It is developed against Codex CLI/App Server 0.154.0; App Server remains experimental.

Verified live, not only by tests: parallel workers on one dependency frontier, dependency unlock, independent verification, the Pipeline Engineer incident path including a closed-code escalation and the user's answer to it, canonical project placement for every created task, and - on this build - a screening session reserved inside a real run, reaching COMPLETED and recording its decision, with the run resumed under the installed 0.11.2 runtime and `doctor` passing after that install.

Not verified live and openly outstanding, in the order that matters for anyone trying this build:

- **A screening that actually hires.** Hiring has run live, and the decision it recorded hired nothing - a verdict, not an omission. A screening that attaches a skill to a worker, and a fetch of a bundle from a public repository, have happened in tests only. That is the half of the feature with the network in it, and it is the part worth trying to break.
- A real multi-hour rate-limit wake-up.
- Host Settings inheritance across all Desktop configurations.
- A clean-Mac install. This build's `install.sh` was run from the repository tree and `doctor` passed afterwards; installing from the release ZIP was last exercised on 0.10.0-beta, and the installer has not changed since apart from its version string. A machine with neither Python nor Codex CLI is untested.
- Computer Use scheduling alongside code work.

Desktop cannot be told that a task started. Its App Server is a separate process from the one Autopilot drives, and the two share only the filesystem, so the sidebar refreshes on the app's own schedule. A created task becomes listable about a second after its turn starts; until the app re-reads, `runtime.desktop_notifications = true` is the only way to learn that work began or finished.

Read [Getting Started](GETTING_STARTED.md), [Project Memory](docs/PROJECT_MEMORY.md), [Architecture](docs/ARCHITECTURE.md), [task graph](docs/DEPENDENCY_GRAPH.md), [parallel execution](docs/PARALLEL_EXECUTION.md), [roles](docs/ROLES.md), [resource locks](docs/RESOURCE_LOCKS.md), [thread naming](docs/THREAD_NAMING.md), [project association](docs/PROJECT_ASSOCIATION.md), [plan evolution and recovery](docs/PLAN_EVOLUTION_AND_RECOVERY.md), [v0.8 → v0.9 migration](docs/MIGRATION_0.8_TO_0.9.md), [skill screening](docs/SKILL_SCREENING.md), [MCP](docs/MCP.md), [Security](docs/SECURITY.md), [Testing](docs/TESTING.md), and [Verification](docs/VERIFICATION.md).
