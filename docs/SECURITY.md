# Security

Workers receive the same effective tools and approvals available to ordinary Codex tasks under App Server `:workspace`. The dispatcher can start, name, read, and interrupt Codex threads; inspect App Server model metadata; read and write `.codex-autopilot/`; and optionally create a Git commit only when `git.auto_commit = true` is explicitly set.

The dispatcher treats every App Server approval request as `BLOCKED` and never sends an approval response. The plugin has no `PermissionRequest` hook. No sandbox bypass, approval bypass, skip-safety flag, unrestricted App Server argument, Codex UI automation, automatic macOS permission grant, or headless execution path exists.

The source archive includes `scripts/live_acceptance.py`, a developer test harness with an explicit flag that can answer only the exact `https://example.com` browser-origin or Google Chrome app-selection request for that test session. It is excluded from the macOS user package and is never imported or called by the production runtime.

Adaptive selects only the two IDs in its local registry and sends them per fresh `thread/start`. It never changes the account or host default model. Missing models, rejected model overrides, unsupported effort metadata, and Sol-only Computer Use requirements become `BLOCKED`; there is no silent model fallback.

Plugin hooks require the normal one-time Codex trust review. The Stop hook starts a dispatcher only after the initiating skill writes a one-time launch request in the initialized project. The prompt hook intercepts only the documented exact control phrases.

Uninstall preserves project state unless the explicit purge flag is supplied. It also preserves other installed Autopilot versions and legacy backups. It does not uninstall Codex, Python, Homebrew, or Git and does not alter user Git or global Codex settings.
