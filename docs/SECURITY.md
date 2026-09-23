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
Python source on this machine. The entry point is `prove_runtime_patch` in
`src/codex_autopilot/runtime_repair.py`, reached through the
`devops-repair-runtime` command. The engineer does not declare a repair; a
gateway proves it. The whole runtime tree is copied to a directory inside the
project's state (the only place the engineer's `:workspace` thread may write),
the edits are applied only in that copy, and the tests run there with every
`CODEX_*` variable removed from the environment. The result reaches the
installation only when all four hold: the reproduction test fails on the
current code, the same test passes with the whole set applied, the rest of the
test suite stays green, and every guarded definition is byte-identical before
and after. `GUARDED_DEFINITIONS` covers desktop ownership, relay executor
identity, dispatcher reservation ownership, incident classification, incident
signature, escalation, the named-action check, and the mandatory healthcheck,
and the on-call's own limits over a stopped task: who may return it and when,
when a stop ticket may close, which patch buys a fresh hire, what an
escalation carries, where advisory tickets go, the owner's answer, and arming;
the hash spans decorators, so a wrapper around a guard counts as a change. If
any of this does not hold, the installation is not touched at all.

A proven set is not written into the installation from the engineer's thread:
that tree is outside the project, and a write there would stop the turn on a
permission request nobody answers. The set is staged in the project
(`.codex-autopilot/runtime-patches/pending/<patch-id>`), the run drains - the
frontier reserves nothing new while a patch waits - and the wake-up, which
launchd runs outside the sandbox, installs it only when no registered run has
a live automatic dispatcher: it copies the current version to
`<version>.repaired-<UTC timestamp>`, checks each module still has the text the
set was proven against (otherwise the patch is refused, set aside and ticketed
for the on-call), writes the set there, and switches `current` with one rename
of a fresh symlink. A process started before the switch keeps the tree it
started from; every later process reads the new one; nobody sees half a set.

`UNPATCHABLE_MODULES` is never patched: `engineer_authority.py`,
`runtime_repair.py`, `hook_trust.py`. Authority, this gateway, and hook trust
are not rewritten by the one that uses them, and a module name differing only
in case is refused too. A repair is a set of edits and applies whole or not at
all; an existing file is never handed over entire - an edit names the fragment
it replaces, and that fragment must occur exactly once in the module. A new
module is the one case where whole content is supplied, and the gateway refuses
it if a file of that name already exists. Every
accepted set is written to `runtime/patches/<patch-id>` with each module's
previous text and a manifest of before/after hashes, and
`devops-revert-runtime-patch` takes the set back together with its test - a
set still staged is withdrawn (kept aside, never deleted), an installed one is
staged as a revert and installed the same atomic way; the
revert refuses when a module changed after the patch was applied. Reinstalling
a version whose `runtime/patches` is not empty renames that installation to
`<version>.repaired-<UTC timestamp>`, prints the path, and leaves it in place;
the archive step skips such directories. The repair reaches only the runtime's own
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

## Hired skills

With screening on, a task may be given a skill fetched from a public repository. A skill is a `SKILL.md` bundle — instructions and the files beside them — and a plugin is not: a plugin registers hooks, MCP servers and commands and lives in the Codex plugin cache, which would change this host's trust surface. **Autopilot installs skills and never plugins**, and that is enforced by construction rather than by rule: `hired_skills.admit_skill_bundle` resolves every destination under `<project>/.codex-autopilot/hired-skills` and refuses any other path, so it cannot name the plugin cache, `$CODEX_HOME/skills`, hooks or MCP configuration; a bundle carrying `.codex-plugin`, `hooks`, `.mcp.json`, `mcp.json`, `plugin.json` or `commands` is refused; symbolic links and files outside the project are refused; and bundle size and file count are bounded. Tests drive the admission path at each of those destinations and require a refusal. Hook trust is untouched by this path, as by every other.

Autopilot also reads `$CODEX_HOME/skills` so a task can be given a skill you already installed. That read is read-only and there is no code path that writes there; a skill of yours is used where it is and never copied into a project. Such a skill is treated as yours rather than as outside material, and the distinction cannot be claimed: provenance comes from where the runtime read the bundle, the requisition protocol has no field that can assert it, an installed skill is named rather than pointed at by path, and a record whose origin disagrees with its provider is refused. One residual risk is named rather than hidden: a worker running Codex's own `skill-installer` during a run could place a skill in your Codex home, which Autopilot would then read as yours, and Autopilot cannot observe or prevent that because it is your Codex under your permission profile.

**Autopilot now fetches skills over the network, and this is the paragraph that says so.** When a screening names a skill that is on neither list, it gives a provider and a locator — it never reaches the network itself, because a Codex turn that raises a permission dialog kills the run: the dispatcher answers no approval and the dialog waits in a task nobody is watching. The fetch is performed by the dispatcher, an ordinary local process outside any Codex turn. It is HTTPS only; it sends no credentials, no authorization header and uses no proxy from the environment, so a source that would need a secret is refused rather than quietly authorized; it accepts only hosts in `runtime.skill_fetch_hosts` (`github.com` and `raw.githubusercontent.com` by default, and an empty list disables fetching entirely); it refuses a redirect away from the URL it asked for, because a redirect can land on a host nobody allowed; it reads one `SKILL.md` and never an archive, so nothing is ever expanded and there is no archive to be a bomb; and it stops reading at 512 KiB while streaming rather than after. Whatever lands is staged inside the project and then passes the same admission as any other bundle. Every failure — offline, a 404, a timeout, a page instead of a skill, a host outside the list — is recorded as an unmet need and the task runs on.

A fetched bundle is external content under R18. It shapes how a worker does the work and never decides whether the work is accepted: it is given to the implementation phase and withheld from the verifier, which is told only which capability was withheld and from which provider. A skill from a repository does not widen what may be executed either — a Skill Pack's deterministic checks may only carry commands the project already runs. What was installed, from where, for which task and on what grounds is recorded in the run's hiring record, counted in the status card, and removed by `codex-autopilot revoke-skill`.

## First-run permissions

The official `codex app-server` child process may need one-time read/write access to the exact `CODEX_HOME` directory because it owns state databases, WAL/SHM files, locks, plugin cache, and temporary command wrappers there. Preflight asks only after an actual permission-shaped failure, reports the path, and exits 77. No state is created before approval.

## Flag audit

Production code and packages contain no safety-bypass, approval-bypass, unrestricted sandbox, or skip-safety option. The normal permission profile is fixed to `:workspace`. The source-only live acceptance harness has dedicated opt-in flags that can answer only the bundled allowlisted memory tools and its exact browser test surface for that developer session. It persists neither approval beyond the session. The harness is excluded from the macOS user ZIP and never imported by production.
