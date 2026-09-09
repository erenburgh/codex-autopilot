# Security

## Authority boundaries

A worker has the ordinary Codex tools available under App Server `:workspace` for the target repository. The dispatcher can create, name, read, and interrupt its Codex tasks; inspect permission/model/project/MCP metadata; read account rate-limit state; and manage files under `.codex-autopilot`. It has no model loop of its own.

The dispatcher treats every App Server approval request as `BLOCKED` and never sends an approval response. The plugin has no permission-request hook. It cannot grant macOS access, trust its own hooks, confirm a destructive operation, operate Codex's UI, or modify account defaults. Its one `memory` MCP tool is explicitly configured with `approval_mode=prompt`; persistent use begins only when the user chooses Codex's `Always` option in a visible initiating task.

Repository commits are disabled by default. The runtime can commit only when a user edits project config to set `git.auto_commit = true`; it does not change Git identity or Git configuration.

## Project Memory boundary

The memory MCP is local stdio. Its project root is the worker process cwd set by App Server. It exposes a fixed tool allowlist with strict schemas and checks all file and artifact paths after symlink resolution. The database must remain under the target repository. It offers no shell, arbitrary filesystem read, raw SQL, network, process control, approval action, or generic proxy.

Truth requires linked non-migration evidence. This protects against accidental promotion, not a malicious worker that fabricates descriptive tool/test evidence. File evidence is server-hashed. All changes receive audit entries and conflicts preserve history.

App Server logs redact prompt text, MCP arguments, structured content, user instructions, and environment probe bodies into hashes and lengths. Operational metadata and IDs remain for recovery and verification. Project memory and logs stay local unless the user shares the repository or files.

## First-run permissions

The official `codex app-server` child process may need one-time read/write access to the exact `CODEX_HOME` directory because it owns state databases, WAL/SHM files, locks, plugin cache, and temporary command wrappers there. Preflight asks only after an actual permission-shaped failure, reports the path, and exits 77. No state is created before approval.

## Flag audit

Production code and packages contain no safety-bypass, approval-bypass, unrestricted sandbox, or skip-safety option. The normal permission profile is fixed to `:workspace`. The source-only live acceptance harness has dedicated opt-in flags that can answer only the bundled allowlisted memory tools and its exact browser test surface for that developer session. It persists neither approval beyond the session. The harness is excluded from the macOS user ZIP and never imported by production.
