# Security

## Authority boundaries

Deterministic local code may manage `.codex-autopilot`, but command hooks never
call Codex App task APIs. The initiating user's instruction authorizes the
complete fixed run. Every automatic App Server launch is bound to the exact
causal predecessor recorded on its reservation; unrelated tasks, caller-
supplied identities, hook prose, and forwarded chat messages cannot replace
that owner.

During bounded preflight only, App Server may answer the exact pending `codex_autopilot_memory.memory` request with `Always` after explicit user authorization and only when Codex advertises that supported option. A separate per-task App Server may perform only the exact no-tools workspace-handoff turn. Each process must fully exit before production. The plugin cannot grant macOS access, trust its own hooks, confirm any other operation, operate Codex's UI, or modify account defaults directly. Its one write-capable `memory` MCP tool uses Codex's `approval_mode=auto`; persistent use begins only when the user authorizes Codex's visible `Always` choice.

Every Desktop-owned production path uses the supported App Server `hooks/list`
inventory as a fail-closed gate. It requires exactly one enabled command hook
for the selected Autopilot plugin's `Stop` event, a `trusted` or `managed`
status, no inventory errors, and the exact stable `current/bin/codex-autopilot`
installed-runtime command. Version and cache refreshes keep this command identity;
only a real hook-definition change should require another review.
`modified` and `untrusted` require explicit `/hooks` review; missing, disabled,
duplicated, erroneous, unknown-status, or cache-relative definitions fail. The
gate runs in preflight and again inside the trusted lifecycle hook immediately
before READY reservation. Hook trust allows local lifecycle state transitions;
it does not authorize Codex App task mutation. The gate never writes hook trust.

An uncertain `thread/start` or `turn/start` result becomes an ambiguous side
effect, retains its reservation, and cannot be repeated automatically. A
definitive App Server failure opens one stable `PIPELINE` incident and moves the
affected reservation to `RETRY_WAIT`. Pipeline Engineer may verify the repair
and re-arm only the original causal owner's dispatcher; it cannot perform the
destination `thread/start`/`turn/start`, call Codex App task APIs, impersonate
the user, change global settings, delete project state, or repeat ambiguous
transport. Legacy Codex App incidents remain readable only for migration.

Since 0.10 Pipeline Engineer may also rewrite the runtime's own installed
Python source on this machine. The entry point is `apply_runtime_patch` in
`src/codex_autopilot/runtime_repair.py`, reached through the
`devops-repair-runtime` command. The engineer does not declare a repair; a
gateway proves it. The whole runtime tree is copied to a temporary directory,
the edits are applied only in that copy, and the tests run there with every
`CODEX_*` variable removed from the environment. The result reaches the
installation only when all four hold: the reproduction test fails on the
current code, the same test passes with the whole set applied, the rest of the
test suite stays green, and every guarded definition is byte-identical before
and after. `GUARDED_DEFINITIONS` covers desktop ownership, relay executor
identity, dispatcher reservation ownership, incident classification, incident
signature, escalation, the named-action check, and the mandatory healthcheck;
the hash spans decorators, so a wrapper around a guard counts as a change. If
any of this does not hold, the installation is not touched at all.

`UNPATCHABLE_MODULES` is never patched: `engineer_authority.py`,
`runtime_repair.py`, `hook_trust.py`. Authority, this gateway, and hook trust
are not rewritten by the one that uses them, and a module name differing only
in case is refused too. A repair is a set of edits and applies whole or not at
all; a file is never handed over entire, an edit names the fragment it
replaces, and that fragment must occur exactly once in the module. Every
accepted set is written to `runtime/patches/<patch-id>` with each module's
previous text and a manifest of before/after hashes, and
`devops-revert-runtime-patch` takes the set back together with its test; the
revert refuses when a module changed after the patch was applied. Reinstalling
a version whose `runtime/patches` is not empty renames that installation to
`<version>.repaired-<UTC timestamp>`, prints the path, and leaves it in place;
the archive step skips such directories. An accepted repair takes effect on the
next dispatched turn, because each turn is a fresh process; nothing is
reinstalled under a running one. The repair reaches only the runtime's own
package: the project's code is the workers' work, and repairing production
quality is on the forbidden list.

Repository commits are disabled by default. The runtime can commit only when a user edits project config to set `git.auto_commit = true`; it does not change Git identity or Git configuration.

Declared deterministic verification commands are executed as argument vectors
in the canonical project directory with a timeout and no shell interpolation.
Artifact checks reject paths that resolve outside the repository. These checks
remain project-plan authority: they do not grant permissions or bypass the
host process sandbox.

Independent verifier prompts contain selected evidence IDs and the task
contract, never the implementer's response or transcript. A `REVISE` handoff
passes only schema-validated issues to a fresh revision task; verifier prose is
not propagated.

## Project Memory boundary

The memory MCP is local stdio. Its project root is the Desktop task's canonical local cwd (or the explicit headless cwd). It exposes a fixed tool allowlist with strict schemas and checks all file and artifact paths after symlink resolution. The database must remain under the target repository. It offers no shell, arbitrary filesystem read, raw SQL, network, process control, approval action, or generic proxy.

Truth requires linked non-migration evidence. Verification results are a separate evidence-linked ledger and never promote themselves to Truth; duplicate causal task/check/thread/turn submissions are accepted only when their complete payload is identical. This protects against accidental promotion, not a malicious worker that fabricates descriptive tool/test evidence. File evidence is server-hashed. All changes receive audit entries and conflicts preserve history.

SQLite writes, migration, generated views, backups, and recovery share a bounded project-local lock. Restore rejects symlinked or differently bound backups, and backup destinations cannot overwrite the live database, WAL/SHM, or lock file.

App Server logs redact prompt text, MCP arguments, structured content, user instructions, and environment probe bodies into hashes and lengths. Operational metadata and IDs remain for recovery and verification. Project memory and logs stay local unless the user shares the repository or files.

## First-run permissions

The official `codex app-server` child process may need one-time read/write access to the exact `CODEX_HOME` directory because it owns state databases, WAL/SHM files, locks, plugin cache, and temporary command wrappers there. Preflight asks only after an actual permission-shaped failure, reports the path, and exits 77. No state is created before approval.

## Flag audit

Production code and packages contain no safety-bypass, approval-bypass, unrestricted sandbox, or skip-safety option. The normal permission profile is fixed to `:workspace`. The source-only live acceptance harness has dedicated opt-in flags that can answer only the bundled allowlisted memory tools and its exact browser test surface for that developer session. It persists neither approval beyond the session. The harness is excluded from the macOS user ZIP and never imported by production.
