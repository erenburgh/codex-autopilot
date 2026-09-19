# Built-in Project Memory MCP

Both profile plugins declare one server named `codex_autopilot_memory` in `.mcp.json`. During installation its command becomes the absolute, version-independent `current/bin/codex-autopilot` launcher. It runs locally over stdio with newline-delimited JSON-RPC and has no listening socket.

During bounded preflight, App Server starts the bundled server and proves its transport, trust, and project binding, then fully exits. Every Desktop-owned worker loads the same installed plugin MCP in its Codex App task with the canonical local project cwd. The server treats process cwd as its project identity, verifies the Git root and database binding, and rejects file or symlink paths that leave that root.

## Allowlisted API

The server exposes one MCP tool named `memory`. Its `operation` field is a strict 16-branch JSON Schema union: `current`, `search`, `get`, `record_evidence`, `record_verified_fact`, `record_verification_result`, `list_verification_results`, `add_observation`, `propose_decision`, `set_decision_status`, `add_constraint`, `question`, `attach_evidence`, `conflict`, `user_correction`, and `milestone_evidence`.

`record_verification_result` writes an audit outcome, not Truth. It requires an existing non-migration Evidence ID plus task, check, verifier thread, and verifier turn identity. Replaying the same causal identity is idempotent only for an identical payload; a changed replay fails closed. `list_verification_results` is task-scoped and uses the same maximum page size of 20 as record retrieval.

Each operation rejects unknown or irrelevant arguments server-side. There is no raw SQL, shell execution, filesystem browsing, network, approval, process-control, or arbitrary tool proxy. MCP resources are empty.

Preflight queries `mcpServerStatus/list`, verifies the tool contract, then calls `mcpServer/tool/call` with `operation=current` to prove the server's project root. The production worker itself must query `current` and fails its evidence gate if the MCP is unavailable; no external App Server writer is attached to repeat that check.

The installed plugin sets `approval_mode=auto` for this tool. Because the single tool includes audited local write operations, its conservative tool-level annotation marks it as write-capable and causes Codex to request approval on first use. The normal installed-server request can offer `session` and `always`; the skill asks the user to choose `Always` for unattended fresh tasks. This is explicit user trust, not dispatcher auto-approval. App Server 0.153.4 exposes no read-only trust-status query, so preflight first observes the actual request. After an authorized `Always` response, it creates a second fresh task and requires an unassisted model-to-MCP call before M1 may start. Production still fails closed if any worker requests approval.

If the MCP process fails during a turn, that turn may fail or be unable to satisfy the evidence gate. The dispatcher will not accept `ROTATE` or `DONE` without new evidence. Recovery uses a fresh worker/process; transparent mid-turn MCP process restart is not promised in this beta.

## Concurrent processes

Every mutation runs in an explicit `BEGIN IMMEDIATE` transaction with transactional sequence allocation. SQLite WAL, full synchronous commits, foreign keys, and a bounded busy timeout remain enabled. A project-local advisory lock serializes writers, schema migration, rendering, backup, and recovery across MCP processes; a process-local guard closes the same-process `flock` gap and has the same bounded wait. OS lock release and SQLite rollback recover a worker that exits mid-transaction without a stale owner record or consumed ID.

Reads use explicit snapshot transactions. Generated Markdown views are built from one snapshot and installed with fsync plus atomic rename while writers are excluded. Backups use SQLite's online backup API into an integrity-checked temporary file, then fsync and atomic rename. Recovery validates project binding and semantic invariants before atomically replacing the database while waiting writers remain excluded.
