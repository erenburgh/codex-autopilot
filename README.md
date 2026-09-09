# Codex Autopilot

Run long Codex projects as verified milestones in serial fresh workers, with evidence-backed local Project Memory.

**Fresh workers prevent context decay; evidence-backed Project Memory prevents context corruption.**

Codex Autopilot is a Codex plugin plus a small non-AI dispatcher. The initiating task turns a goal into milestones. For each milestone the dispatcher creates one durable Codex task, waits for its official App Server `turn/completed` event, validates new milestone evidence, retires that worker, and creates the next fresh task. The dispatcher does not call a model while it waits, rotates workers, checks state, or handles rate-limit timers.

Project Memory is a project-local SQLite/FTS5 database exposed to workers through a bundled stdio MCP server. Verified facts require explicit evidence; observations, decisions, constraints, questions, evidence, and conflicts remain separate record types. Each fresh worker receives a bounded set of IDs and constraints and retrieves details on demand instead of inheriting an expanding transcript.

## Install

Requirements: macOS, Codex Desktop, a signed-in official Codex CLI with App Server, Python 3.11 or newer, an eligible account, and an existing Git repository for the target project.

```bash
git clone https://github.com/erenburgh/codex-autopilot.git
cd codex-autopilot
./install.sh --profile adaptive --install-deps
```

The macOS release ZIP can be extracted instead. `--install-deps` uses an existing Homebrew installation when Python is missing and npm when Codex CLI is missing. The installer never installs Homebrew itself.

After installation, start a fresh Codex task so the plugin loads. First use has two explicit Codex trust steps: approve the two Autopilot hooks in `/hooks`, then let the skill call the single local `memory` tool with `operation=current` and choose `Always`. Autopilot never answers either approval itself. The first run then performs deterministic preflight. If Codex blocks the official App Server from its state directory, preflight names the exact `CODEX_HOME` path that needs one-time read/write approval, exits with code 77, and creates no project run-state.

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

- `Pause Codex Autopilot.`
- `Resume Codex Autopilot.`
- `What is Codex Autopilot doing right now?`
- `Uninstall Codex Autopilot.`

## Persistent state

- `.codex-autopilot/plan.json`: canonical execution plan.
- `.codex-autopilot/run-state.json`: canonical orchestration journal.
- `.codex-autopilot/memory.sqlite3`: canonical project knowledge and evidence.
- `.codex-autopilot/memory-backups/latest.sqlite3`: last verified milestone backup.
- `ROADMAP.md`, `PROJECT_STATE.md`, and `DECISIONS.md`: human-readable views.
- `.codex-autopilot/MILESTONE.md`: current worker cache.
- `.codex-autopilot/HANDOFF.md`: short advisory note; never treated as evidence.

## Safety and current limits

Workers use App Server `:workspace`. The dispatcher does not answer approvals, change global Codex settings, change Git configuration, grant permissions, or auto-commit by default. The memory server exposes one tool with a strict union of 14 operations; Project Memory has no network service, shell tool, raw SQL tool, embedding service, or external database.

The v0.8 beta supports macOS. It is developed against Codex CLI/App Server 0.153.4; App Server remains experimental. Saved Project placement, a real multi-hour rate-limit wake-up, Host Settings inheritance across all Desktop configurations, and an external clean-Mac v0.8 run remain beta verification items.

Read [Getting Started](GETTING_STARTED.md), [Project Memory](docs/PROJECT_MEMORY.md), [Architecture](docs/ARCHITECTURE.md), [MCP](docs/MCP.md), [Security](docs/SECURITY.md), and [Verification](docs/VERIFICATION.md).
