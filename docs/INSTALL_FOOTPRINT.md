# Install and uninstall footprint

The installer creates `~/Library/Application Support/CodexAutopilot/0.7.0-beta/`, an internal venv, runtime source, plugins, and user docs, then updates `current` to that directory. Installation does not remove an existing `0.6.0-beta` directory. It registers one local Codex marketplace and one selected profile plugin.

It does not add PATH entries, modify shell profiles or Git config, create or commit repositories, change account/default model or reasoning, change sandbox/network/approval settings, modify MCP servers or unrelated plugins, grant macOS permissions, or add a launch agent. Adaptive sends a per-worker model and effort through App Server; it does not change global settings. `--install-deps` may install missing Python through Homebrew and the official Codex CLI through npm; uninstall leaves those shared tools installed.

Old local `astra-autopilot-adaptive` and `astra-autopilot-inherit` preview skills are moved to `legacy-backups` only when found.

`codex-autopilot uninstall --yes` first pauses an identifiable active dispatcher and cancels removal if it does not stop within 35 seconds. It removes the v0.7 plugin registrations, local marketplace registration, `0.7.0-beta` runtime directory, and `current` symlink when it points to v0.7. Other installed version directories, including `0.6.0-beta`, and legacy backups remain. Project source and Git remain. Project state is deleted only with `--purge-project-state --project <path>`.
