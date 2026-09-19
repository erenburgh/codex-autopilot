# Install and uninstall footprint

## Default installation

`install.sh` creates or replaces the directory of the version it installs (`0.10.0-beta` in this release), and writes one launch agent outside it:

```text
~/Library/Application Support/CodexAutopilot/
├── <version>/
│   ├── venv/                   # Python venv without pip
│   ├── runtime/                # the repository tree: src/, tests/, docs/,
│   │                           # scripts/, plugins/, install.sh, pyproject.toml
│   ├── bin/codex-autopilot
│   ├── plugins/
│   ├── .agents/
│   └── user documentation
├── <version>.repaired-<stamp>/ # only when the replaced install carried accepted repairs
├── legacy-backups/
│   └── previous-installs-<stamp>.zip   # earlier installations, zipped and then removed
├── projects.json               # projects the wake-up sweep visits; written when a run is armed
├── wake-sweep.log              # output of the wake-up agent
└── current -> <version>

~/Library/LaunchAgents/
└── com.codex-autopilot.wake.plist   # wake-up agent: RunAtLoad plus StartInterval 300
```

`runtime/` is the repository tree, not `src` alone: `tests/` is copied next to it because the on-call engineer proves a repair by running that suite against the installed copy.

It registers a local marketplace named `codex-autopilot-local` through the official CLI and installs exactly one of `codex-autopilot-adaptive` or `codex-autopilot-host-settings`. Codex owns the resulting registry/cache data under its normal home. The installed profile's `.mcp.json` points to the absolute `current/bin/codex-autopilot` launcher, not to the version directory.

When old local preview skills named `astra-autopilot-adaptive` or `astra-autopilot-inherit` exist under `~/.codex/skills`, the installer moves them into `~/Library/Application Support/CodexAutopilot/legacy-backups/` instead of deleting them.

With `--install-deps`, missing Python may be installed with `brew install python@3.13` when Homebrew already exists. A missing Codex CLI may install Node through Homebrew and `@openai/codex` through npm. Homebrew itself is never installed. These shared dependencies are not removed by Autopilot uninstall.

## The wake-up agent

The installer writes `~/Library/LaunchAgents/com.codex-autopilot.wake.plist` and loads it with `launchctl bootstrap gui/<uid>`. The agent runs `codex-autopilot _wake-sweep` once at login and then every 300 seconds, for as long as it stays installed, and appends its output to `~/Library/Application Support/CodexAutopilot/wake-sweep.log`.

One sweep reads the project list from `projects.json` and decides per project. A project whose `.codex-autopilot/config.toml` is gone, whose run is paused, `BLOCKED` or `DONE`, or which has no task waiting for a retry, is skipped. Where a task waits in `RETRY_WAIT` with a due retry time and no live wake-up process is already waiting for it, the sweep arms one. That process sleeps until the time comes, passes the same hook-trust and ownership gate as a hook-driven launch, and then raises the dispatcher. The sweep itself starts no worker, makes no model request, and wakes no run a human stopped. It exists because the sleeping wake-up process does not survive a reboot.

`CODEX_AUTOPILOT_SKIP_LAUNCHD=1` at install time still writes the plist but does not load it into launchd. `codex-autopilot uninstall --yes` boots the agent out and deletes the plist.

## Previous installations

Earlier installations are not left standing beside the new one. Once the new version is in place, every other versioned directory in the install root is added to `legacy-backups/previous-installs-<stamp>.zip` and then removed; the installer prints how many were packed and where the archive is. `current`, `legacy-backups` and the version being installed are left alone.

One directory escapes this. An installation that carries accepted runtime repairs - a non-empty `runtime/patches` - is renamed to `<version>.repaired-<stamp>` beside itself before the new install replaces it, the installer prints that path, and the archive loop skips it. Nothing in it is deleted or zipped.

## Project footprint

After successful preflight and initialization, the target Git repository receives:

```text
ROADMAP.md
.codex-autopilot/
├── config.toml
├── plan.json
├── run-state.json
├── MILESTONE.md
├── HANDOFF.md
├── PROJECT_STATE.md
├── DECISIONS.md
├── memory.sqlite3             # WAL/SHM may exist while open
├── memory-backups/latest.sqlite3
├── logs/
└── migrations/               # only when migrating old state
```

Preflight creates no project run-state when it fails. Its disposable SQLite/FTS5 probe is removed.

## What is unchanged

Installation does not add PATH entries, edit shell profiles, edit Git config, initialize or commit a repository, alter account defaults, change Codex model/reasoning/speed/sandbox/network/approval settings, grant macOS permissions, or modify unrelated plugins. The one thing it adds to the system outside its own directory is the wake-up launch agent described above. Adaptive sends only per-worker model and effort fields. Host Settings sends neither.

## Uninstall

`codex-autopilot uninstall --yes` pauses an identifiable dispatcher of its own version, removes the two plugin registrations and the marketplace registration, boots out and deletes the wake-up launch agent, removes its own version directory, removes `current` only when it points there, and clears the temporary launch registry (that directory is still named `codex-autopilot-<uid>-v0.8`, and the name is the only thing about it that is still v0.8). It preserves the rest of the install root - `legacy-backups`, any `<version>.repaired-<stamp>` tree, `projects.json`, `wake-sweep.log`, and any older installation still present - along with shared tools, source repositories, and every project's state.

Project state is set aside only with `--purge-project-state --project <absolute-path>`. This moves that project's `.codex-autopilot` directory to a sibling `.codex-autopilot.purged-<stamp>` (the path is printed) and never deletes it: removing state without a restorable snapshot is refused by rule R28, so freeing the space is your explicit `rm -rf` of that sibling. It does not touch `ROADMAP.md`, project source, Git metadata, or commits. Likewise `start-skill --replace` copies the previous state to `.codex-autopilot.replaced-<stamp>` before overwriting it, and records that path in the new run's journal.
