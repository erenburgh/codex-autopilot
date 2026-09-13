# Codex Autopilot

Codex Autopilot turns large projects into a dependency-aware workflow of fresh
specialist workers. Independent work can run in parallel; dependent work waits
for verified prerequisites; important results can be checked by fresh
independent verifiers; and Project Memory carries evidence-backed context
without entire old sessions. The scheduler is deterministic code, not a
persistent manager LLM.

**Fresh workers prevent context decay; evidence-backed Project Memory prevents context corruption.**

Codex Autopilot is a Codex plugin plus a deterministic dependency scheduler. The
initiating task turns a goal into tasks. In the v0.9 `desktop_owned` surface,
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

Requirements: macOS, Codex Desktop, a signed-in official Codex CLI with App Server, Python 3.11 or newer, an eligible account, and an existing Git repository for the target project.

```bash
git clone https://github.com/erenburgh/codex-autopilot.git
cd codex-autopilot
./install.sh --profile adaptive --install-deps
```

The macOS release ZIP can be extracted instead. `--install-deps` uses an existing Homebrew installation when Python is missing and npm when Codex CLI is missing. The installer never installs Homebrew itself.

After installation, start a fresh Codex task so the plugin loads. First use has two explicit Codex trust steps: open `/hooks` and trust the current Autopilot **Stop** hook, then let the dedicated preflight task call the single local `memory` tool with `operation=current` and choose `Always`. Autopilot never answers either approval itself. Installed hook and MCP definitions use the permanent `current/bin/codex-autopilot` entrypoint, so reinstalling or refreshing the plugin does not change their command identity; another hook review is required only when the hook definition itself changes. Preflight verifies that the exact selected-plugin Stop hook is enabled, trusted (or managed), and points to that stable runtime before project initialization or Worker 1; modified or untrusted requires review, while missing, disabled, duplicated, or erroneous inventory fails closed. If Codex blocks the official App Server from its state directory, preflight names the exact `CODEX_HOME` path that needs one-time read/write approval, exits with code 77, and creates no project run-state.

## Use

In Codex, ask Autopilot to work on an existing Git repository. The target can differ from the initiating task directory:

```text
Use Codex Autopilot for /absolute/path/to/my-project.

Goal: Build the complete inventory system.
Break it into independently verifiable milestones and continue until DONE.
```

The Adaptive profile uses `auto` by default:

- GPT-5.6 Sol handles milestones verifiable with code, files, shell tools, logs, tests, and builds.
- GPT-6 Astra handles milestones whose Definition of Done requires real browser or desktop GUI interaction through Computer Use.
- Reasoning is independent: `medium`, `high`, `xhigh`, or `max`.

Explicit strategies are `Sol only` and `Astra only`. The Host Settings profile sends neither a model nor an effort field; App Server applies its current defaults to every fresh task.

Exact no-model controls are:

- `status` - a few lines: progress, what is running, what blocks it.
- `status detail` - the full report: every task, its state, and the reason it is waiting.
- `Pause Codex Autopilot.`
- `Resume Codex Autopilot.` It is also the user's answer to an escalation: it
  closes an incident the Pipeline Engineer handed over and returns a task whose
  worker is no longer alive to retry.
- `Uninstall Codex Autopilot.`

Each control is answered by the hook itself, without a model turn. Codex marks
such an answer as a blocked message: that label means the hook replied instead
of the model, not that something failed.

## Persistent state

- `.codex-autopilot/plan.json`: canonical execution plan.
- `.codex-autopilot/run-state.json`: canonical orchestration journal.
- `.codex-autopilot/memory.sqlite3`: canonical project knowledge and evidence.
- `.codex-autopilot/memory-backups/latest.sqlite3`: last verified milestone backup.
- `ROADMAP.md`, `PROJECT_STATE.md`, and `DECISIONS.md`: human-readable views.
- `.codex-autopilot/MILESTONE.md`: current worker cache.
- `.codex-autopilot/HANDOFF.md`: short advisory note; never treated as evidence.

## Safety and current limits

Workers use the configured `:workspace` App Server permission profile. App Server preflight and automatic worker processes never answer approval requests; an approval request fails closed. Autopilot does not call Codex App task APIs, change global Codex settings, change Git configuration, grant permissions, or auto-commit by default. The memory server exposes one tool with a strict operation union; Project Memory has no network service, shell tool, raw SQL tool, embedding service, or external database.

The v0.8 beta supports macOS. It is developed against Codex CLI/App Server 0.154.0; App Server remains experimental.

Verified live, not only by tests: parallel workers on one dependency frontier, dependency unlock, independent verification, the Pipeline Engineer incident path including a closed-code escalation and the user's answer to it, and canonical project placement for every created task.

Not verified live and openly outstanding: a real multi-hour rate-limit wake-up, Host Settings inheritance across all Desktop configurations, an external clean-Mac install (the release ZIP itself installs and passes `doctor` on a machine that already has Codex; a machine with neither Python nor Codex CLI is untested), and Computer Use scheduling alongside code work.

Desktop cannot be told that a task started. Its App Server is a separate process from the one Autopilot drives, and the two share only the filesystem, so the sidebar refreshes on the app's own schedule. A created task becomes listable about a second after its turn starts; until the app re-reads, `runtime.desktop_notifications = true` is the only way to learn that work began or finished.

Read [Getting Started](GETTING_STARTED.md), [Project Memory](docs/PROJECT_MEMORY.md), [Architecture](docs/ARCHITECTURE.md), [v0.9 task graph](docs/DEPENDENCY_GRAPH.md), [parallel execution](docs/PARALLEL_EXECUTION.md), [roles](docs/ROLES.md), [resource locks](docs/RESOURCE_LOCKS.md), [thread naming](docs/THREAD_NAMING.md), [project association](docs/PROJECT_ASSOCIATION.md), [plan evolution and recovery](docs/PLAN_EVOLUTION_AND_RECOVERY.md), [v0.8 → v0.9 migration](docs/MIGRATION_0.8_TO_0.9.md), [MCP](docs/MCP.md), [Security](docs/SECURITY.md), [Testing](docs/TESTING.md), and [Verification](docs/VERIFICATION.md).
