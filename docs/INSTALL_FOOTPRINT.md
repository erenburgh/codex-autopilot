# Install and uninstall footprint

## Default installation

`install.sh` creates or replaces only the v0.8 directory:

```text
~/Library/Application Support/CodexAutopilot/
├── 0.6.0-beta/                 # preserved when present
├── 0.7.0-beta/                 # preserved when present
├── 0.8.0-beta/
│   ├── venv/                   # Python venv without pip
│   ├── runtime/src/
│   ├── bin/codex-autopilot
│   ├── plugins/
│   ├── .agents/
│   └── user documentation
└── current -> 0.8.0-beta
```

It registers a local marketplace named `codex-autopilot-local` through the official CLI and installs exactly one of `codex-autopilot-adaptive` or `codex-autopilot-host-settings`. Codex owns the resulting registry/cache data under its normal home. The installed profile's `.mcp.json` points to the absolute v0.8 launcher.

When old local preview skills named `astra-autopilot-adaptive` or `astra-autopilot-inherit` exist under `~/.codex/skills`, the installer moves them into `~/Library/Application Support/CodexAutopilot/legacy-backups/` instead of deleting them.

With `--install-deps`, missing Python may be installed with `brew install python@3.13` when Homebrew already exists. A missing Codex CLI may install Node through Homebrew and `@openai/codex` through npm. Homebrew itself is never installed. These shared dependencies are not removed by Autopilot uninstall.

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

Installation does not add PATH entries, edit shell profiles, edit Git config, initialize or commit a repository, alter account defaults, change Codex model/reasoning/speed/sandbox/network/approval settings, grant macOS permissions, install a launch agent, or modify unrelated plugins. Adaptive sends only per-worker model and effort fields. Host Settings sends neither.

## Uninstall

`codex-autopilot uninstall --yes` pauses an identifiable v0.8 dispatcher, removes the two v0.8 plugin registrations and marketplace registration, removes `0.8.0-beta`, removes `current` only when it points to v0.8, and clears the v0.8 temporary launch registry. It preserves v0.6, v0.7, legacy backups, shared tools, source repositories, and every project's state.

Project state is removed only with `--purge-project-state --project <absolute-path>`. This deletes that project's `.codex-autopilot` directory; it does not delete `ROADMAP.md`, project source, Git metadata, or commits.
