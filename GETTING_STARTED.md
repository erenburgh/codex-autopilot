# Getting Started

## Requirements

- macOS.
- Codex Desktop.
- The official Codex CLI, signed in, with `codex app-server` available.
- An eligible ChatGPT/Codex account.
- Python 3.11 or newer.
- An existing Git repository for the project Autopilot will change.

Normal use does not require Python commands, pip, a manual venv, PATH changes, or manual state editing. Homebrew is optional. Git must exist because the target must already be a repository; Autopilot does not run `git init`.

## Install once

Clone the repository or extract the macOS release ZIP, then run:

```bash
./install.sh --profile adaptive --install-deps
```

Use `--profile host-settings` if every fresh worker should use App Server host defaults with no model or effort override.

The installer:

1. checks macOS, Python 3.11+, `codex app-server`, and Codex login;
2. optionally installs missing Python through an existing Homebrew and missing Codex CLI through npm;
3. creates `~/Library/Application Support/CodexAutopilot/0.8.0-beta/` with a pip-free venv, runtime, docs, and both profile bundles;
4. replaces the memory MCP launcher placeholder with the absolute, versioned v0.8 runtime path;
5. updates the `current` symlink;
6. registers the local marketplace and exactly one selected profile through `codex plugin`.

It preserves installed `0.6.0-beta` and `0.7.0-beta` directories. It does not modify PATH, shell profiles, Git config, repository history, global Codex model/reasoning/sandbox/network/approval settings, macOS settings, or unrelated plugins.

Open `/hooks` in Codex, inspect the two Autopilot commands, and trust them once. Hook trust is a normal Codex security step and the installer cannot bypass it.

Start a fresh Codex task after installation. On the first Autopilot request, the skill calls the bundled local `memory` tool with the harmless `operation=current` probe. Codex offers `session` and `always`; choose **Always** if you want later fresh workers to use Project Memory unattended. This is one tool with 14 strict operations, rather than 14 separately approved tools. The dispatcher cannot click or answer this prompt. If you choose session-only, a fresh worker can stop at an MCP approval because that choice does not cross task boundaries.

## Start

In any Codex task, name the target repository explicitly:

```text
Use Codex Autopilot for /absolute/path/to/project.

Goal:
Build the complete inventory system.

Break it into independently verifiable milestones and continue autonomously until DONE.
```

The initiating model inspects the target, writes a small bootstrap plan, and runs the bundled `start-skill` helper. You do not write `ROADMAP.md` by hand. The helper validates the plan and prints preflight results such as:

```text
Target: OK
Git: OK
Runtime: OK
App Server: OK
Worker access: OK
Project Memory: OK
Memory tool trust: USER CONTROLLED
Model metadata: OK
Preflight: PASS
```

Only after PASS does it create the project state, human-readable views, and a short-lived launch request. The trusted Stop hook waits for the initiating turn's durable completion, then starts the dispatcher. Worker 1 appears as a fresh, durable Codex task.

Preflight confirms transport, tool schema, FTS5, and target binding. Current App Server has no read-only method that reveals whether the user selected persistent `Always` trust, so the initiating skill performs the explicit tool probe before start and the preflight prints the reminder rather than claiming to verify that preference.

### First-run access

The official App Server stores its state, SQLite WAL/SHM files, locks, plugin cache, and temporary wrappers under `CODEX_HOME` (normally `~/.codex`). In a restricted initiating task, macOS/Codex may block that child process. Preflight then prints `Worker access: APPROVAL REQUIRED`, names the exact directory, exits with code 77, and leaves the target uninitialized. Approve read/write access to that exact directory through Codex's normal permission UI, then repeat the same start request. Autopilot does not inspect App Server databases and does not request broad disk access.

## What runs

At most one Autopilot model turn is active. Each worker completes one milestone, records evidence through the local memory MCP, writes a short handoff, and returns `ROTATE`, `DONE`, or `BLOCKED`. Adaptive also supports reasoning `ESCALATE` and AUTO-only Sol-to-Astra `REQUIRE_COMPUTER_USE`. The dispatcher waits for `turn/completed` before advancing or starting another worker.

## Pause, resume, and inspect

- Send `Pause Codex Autopilot.` to interrupt the active worker and preserve the unfinished milestone.
- Send `Resume Codex Autopilot.` to reconcile durable state and create a fresh worker for unfinished work.
- Send `What is Codex Autopilot doing right now?` for deterministic local status without a model request.

After a dispatcher crash or reboot, resume is manual. If a worker completed and the dispatcher crashed before recording it, reconciliation reads the durable App Server turn, validates the checkpoint and memory evidence, and advances once. `milestone_completions` and the run journal protect completed milestones from routine duplicate advancement.

## Advanced local CLI

The executable is:

```text
~/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot
```

It is intentionally not added to PATH. Commands include `preflight`, `doctor`, `status`, `logs`, `stop`, `resume`, `run`, `test desktop`, and `uninstall`.

## Uninstall

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" uninstall --yes
```

This removes v0.8 plugin registrations, the v0.8 runtime, a `current` symlink that points to v0.8, and the v0.8 temporary launch registry. It preserves v0.6, v0.7, shared Python/Codex/Homebrew/Git installations, legacy backups, source repositories, and project state.

To remove one project's Autopilot state too:

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" uninstall --yes --purge-project-state --project /absolute/path/to/project
```
