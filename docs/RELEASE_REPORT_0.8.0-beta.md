# Codex Autopilot v0.8.0-beta release report

This report describes the final release candidate. Archive SHA-256 values are generated beside the ZIP files and are reported with the release artifacts rather than embedded in an archive that they hash.

## First run and lifecycle

1. **Clean-machine root cause.** The official `codex app-server` child process must read and write its own state below `CODEX_HOME` (normally `~/.codex`), including SQLite/WAL/SHM state, locks, plugin cache, and temporary wrappers. A restricted initiating Codex task could not grant that child process the needed path, so v0.7 could stop at IDLE with zero workers. Autopilot never needed to read Codex's internal database itself.
2. **Fix.** `start-skill` now runs a deterministic preflight before project initialization. A permission-shaped App Server failure exits 77, names the exact directory, and leaves no run-state. A short-lived per-user launch registry also carries an explicit target root from an initiating task in another cwd to the trusted Stop hook.
3. **Permissions.** Workers use `:workspace` at the target repository. The official App Server may need one-time read/write access to the exact `CODEX_HOME`. Users separately review hook trust in `/hooks` and choose whether to grant persistent trust to the single local `memory` tool. Autopilot cannot grant either permission.
4. **Hooks.** `/hooks` is still required. Stop starts the dispatcher after the initiating turn completes; UserPromptSubmit handles exact control phrases. The plugin validator requires hook discovery through the plugin directory, not a top-level manifest field.
5. **Initiating cwd.** The initiating task may be outside the target repository. The skill resolves an explicit absolute `--project`, preflight verifies it, and Worker 1 uses that target as App Server cwd.
6. **Target root.** The skill resolves the user's requested existing Git repository and passes it explicitly. The runtime canonicalizes it with `Path.resolve()`, verifies the directory and `.git`, and binds state and MCP to it.

The invariant remains one milestone per durable visible Codex task, one active Autopilot model turn at a time, and no controller model. The local dispatcher, hooks, retry timers, checkpoint gates, state rendering, and Project Memory server are deterministic code and spend no model allowance while waiting.

## Project Memory

7. **Architecture.** Project Memory is a bundled local SQLite/FTS5 evidence store exposed to workers through one local stdio MCP server. Every fresh worker gets a fresh MCP process attached to the same project database.
8. **Physical database.** `<project>/.codex-autopilot/memory.sqlite3`; the latest verified online backup is `<project>/.codex-autopilot/memory-backups/latest.sqlite3`.
9. **Schema.** Schema version 1 contains `schema_meta`, `sequences`, `records`, `evidence`, `record_evidence`, `milestone_evidence`, `milestone_completions`, `conflicts`, `conflict_history`, `audit_log`, `records_fts`, and three FTS synchronization triggers.
10. **MCP lifecycle.** The plugin declares the installed absolute v0.8 launcher. App Server starts the stdio process with the explicit project cwd during preflight and per worker, owns its shutdown, and starts another process for the next worker. The dispatcher checks connection, contract, root binding, and initialization before a model turn.
11. **MCP surface.** One tool named `memory` exposes 14 strict operations: `current`, `search`, `get`, `record_evidence`, `record_verified_fact`, `add_observation`, `propose_decision`, `set_decision_status`, `add_constraint`, `question`, `attach_evidence`, `conflict`, `user_correction`, and `milestone_evidence`. Unknown fields are rejected. There is no raw SQL, shell, network, arbitrary filesystem browser, process control, or resource endpoint.
12. **Truth.** A `FACT-*` record is a verified factual claim. Creation requires at least one existing supporting evidence record whose kind is not `migration`.
13. **Evidence.** `EVID-*` is a first-class record with kind, summary, provenance, optional file/range/hash, command/result/exit code, tool, artifact, user instruction, or environment probe. File paths must resolve inside the project and the server computes their SHA-256.
14. **Decisions.** `DEC-*` records keep origin, status, reason, scope, author, provider IDs, references, and supersession. Agent proposals remain distinct from user decisions and verified facts.
15. **Constraints.** `CON-*` records preserve origin, scope, status, and provenance. Only a bounded critical subset enters a worker's bootstrap.
16. **Questions.** `Q-*` records represent open or resolved unknowns and can identify the milestone that needs an answer. Resolution does not automatically create Truth.
17. **Observations.** `OBS-*` records are unverified agent notes with confidence and provenance. They never promote themselves to Truth.
18. **Conflicts.** `CONFLICT-*` links an existing record to incoming evidence or a record, retains status and resolution, and appends immutable conflict-history actions. Conflicting evidence does not overwrite Truth.
19. **User correction.** The correction operation preserves existing factual history, can reject or supersede an agent decision, records a user-origin desired state, and opens a conflict when the desired state disagrees with verified project reality.
20. **Promotion.** Observation may lead to a proposed Decision; a Question may be resolved after verification; Decision never becomes Truth automatically; Truth always requires validated evidence. Model confidence and prose do not count.
21. **Handoff.** `HANDOFF.md` is an adjacent worker note limited to five fields and 8 KiB. It is advisory, never evidence, and never canonical Truth.
22. **Canonical files.** `plan.json`, `run-state.json`, and `memory.sqlite3` are canonical. The database backup is a recovery artifact.
23. **Human views and caches.** `ROADMAP.md` is the readable execution view; `MILESTONE.md` is the current-worker cache; `PROJECT_STATE.md` and `DECISIONS.md` are generated from SQLite; `HANDOFF.md` is advisory.
24. **v0.7 migration.** Before mutation the complete old state is copied to a timestamped migration backup. Only an identical completed milestone prefix is preserved. Durable completion becomes migration evidence; old Decisions become agent-origin proposals; old handoff/project-state prose becomes low-confidence Observations; zero prose becomes Truth.

## Design reference and context control

25. **Agent Memory Engine study.** The read-only design review used `uudam42/agent-memory-engine` at commit `146044dfae3143c1028c0a6b78e193ccfee64802` and examined its local-first storage, FTS5, evidence, candidate/promotion, conflict, retrieval-budget, isolation, lifecycle, MCP, and safety concepts.
26. **Ideas used.** v0.8 uses the concepts of local SQLite/FTS5, explicit provenance, staged record types, non-destructive conflicts, selective retrieval, project isolation, and stdio MCP. It implements a smaller milestone-specific schema and lifecycle.
27. **Copied code.** None. No AME module, schema, test, README text, package, service, or runtime dependency is present.
28. **Attribution.** Because no source fragment is distributed, no third-party source notice is required. The design reference and pinned commit are documented for transparency.
29. **Context benchmark.** M1/M5/M10/M20 real prompt-builder sizes are 3576/3821/3828/3828 characters, approximately 894/956/957/957 tokens. The M1→M20 increase is 252 characters. The labeled synthetic v0.7 full-history baseline grows from 5079 to 54244 characters; it is not a live v0.7 measurement.
30. **Twenty-milestone integrity.** Deterministic coverage creates 200 records across 20 milestones and verifies that Observations never cross the Truth boundary.
31. **Summary drift.** Repeated changed handoff prose does not mutate the canonical fact or its evidence link.
32. **Conflict test.** Contradictory evidence opens a conflict and preserves the prior Truth and history.

## Acceptance, limits, and release

33. **External onboarding.** v0.7 was externally exercised on another Mac and exposed the original permission bug. v0.8 has local clean-state and real restricted-access coverage; the final packaged first-use flow still needs repetition on an external clean Mac.
34. **Live workers.** Final installed-runtime runs passed Sol-only three-worker rotation, AUTO Sol→Astra→Sol with real browser Computer Use, AUTO Sol→Astra capability escalation, and Host Settings field omission. All used durable fresh task IDs and showed no overlapping active turns.
35. **Automated tests.** The final suite contains 75 tests; final pass status is recorded in `VERIFICATION.md` and the release output.
36. **Known limits.** App Server is experimental; Saved Project placement is unverified; Host Settings proves field omission but not all-host inheritance; external clean-Mac v0.8 onboarding, weekly exhaustion, and a complete multi-hour retry remain unverified; reboot resume is manual; mid-turn MCP restart is not promised; semantic contradiction detection is intentionally explicit rather than universal.
37. **Install command.** From the extracted source or macOS release tree: `./install.sh --profile adaptive --install-deps`. Use `--profile host-settings` for host defaults.
38. **First use.** Start a fresh Codex task and say `Use Codex Autopilot for this project.` Review the two plugin hooks in `/hooks`. When the skill calls `memory(operation=current)`, choose `Always` only if the installed local plugin is trusted; session-only trust will not cover fresh workers.
39. **Release archives.** The build produces `codex-autopilot-0.8.0-beta-macos.zip`, `codex-autopilot-0.8.0-beta-source.zip`, and one `.sha256` sidecar for each. The macOS archive excludes development harnesses and tests; the source archive includes them.
40. **SHA-256.** Use the generated `.sha256` sidecars supplied with the release. They are produced after the final clean archive build and are the authoritative values.

## Release verdict

The implementation meets the v0.8 core invariants and local acceptance criteria. Its honest release status is **public beta candidate**: local deterministic and real App Server/model/Computer Use acceptance pass, while an external clean-Mac v0.8 onboarding run remains the principal beta verification item.
