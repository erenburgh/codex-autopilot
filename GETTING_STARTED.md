# Getting Started

## Requirements

- macOS
- Codex Desktop and a signed-in Codex CLI/App Server
- an eligible ChatGPT/Codex plan
- an existing Git repository

The installer uses Python 3.11 or newer internally. With `--install-deps`, it can install Python through an existing Homebrew installation and the official Codex CLI through npm. Normal use does not require Python commands, a venv path, or manual state editing.

## Install once

```bash
git clone <repository-url> codex-autopilot
cd codex-autopilot
./install.sh --profile adaptive --install-deps
```

The macOS ZIP can be extracted instead of cloning. The installer creates `~/Library/Application Support/CodexAutopilot/0.7.0-beta`, updates the `current` symlink, creates an internal venv, and installs exactly one profile plugin. Existing version directories, including 0.6.0-beta, are not removed by installation.

In Codex, open `/hooks`, inspect the two Codex Autopilot commands, and trust them once. The installer does not bypass hook trust.

## Start with AUTO

Open your existing Git project in Codex and send:

```text
Use Codex Autopilot for this project.

Goal:
Build the complete inventory system.

Break the work into independently verifiable milestones and continue autonomously until DONE.
```

The planner classifies each milestone by whether its Definition of Done requires Computer Use. Code milestones use Sol. GUI milestones use Astra. Reasoning is selected separately.

For a Computer Use milestone, keep Codex Desktop open. On the currently verified Desktop build, the worker task must be foreground before its in-app browser surface is available. Codex Autopilot never answers approval prompts on your behalf; if the required app or browser permission is unavailable, the run stops as `BLOCKED`.

## Select one model explicitly

Use `Use Codex Autopilot with Sol only for this project.` to keep every code milestone on Sol. A computer-use milestone becomes `BLOCKED` instead of silently switching models.

Use `Use Codex Autopilot with Astra only for this project.` to run every milestone on Astra. This is an explicit allowance tradeoff.

## Pause, resume, and status

Send `Pause Codex Autopilot.` to interrupt the active worker and keep the milestone unfinished. Send `Resume Codex Autopilot.` to create a fresh worker for it. The same resume phrase recovers after dispatcher failure or reboot.

Send `What is Codex Autopilot doing right now?` to see milestone, strategy, execution mode, selected model, reasoning, selection reason, phase, and dispatcher state without a model request.

## Advanced interface

The executable is `~/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot`. It provides `doctor`, `status`, `logs`, `stop`, `resume`, `run`, `test desktop`, and `uninstall`. It is intentionally not added to PATH.

`codex-autopilot uninstall --yes` removes the v0.7 plugin registrations, v0.7 runtime, and its `current` symlink. Other installed version directories, including 0.6.0-beta, and legacy backups remain. Project state remains unless `--purge-project-state --project <path>` is explicitly supplied.
