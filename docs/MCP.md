# Built-in Project Memory MCP

Both profile plugins declare one server named `codex_autopilot_memory` in `.mcp.json`. During installation its command becomes the absolute versioned v0.8 launcher. It runs locally over stdio with newline-delimited JSON-RPC and has no listening socket.

For preflight and every worker, App Server starts the bundled server. App Server 0.153.4 replaces a named MCP entry when thread config is supplied, so the dispatcher repeats the complete installed local stdio transport and sets its `cwd` to the explicit target repository. The command remains the same versioned Autopilot launcher declared by the plugin. The server treats process cwd as its project identity, verifies the Git root and database binding, and rejects file or symlink paths that leave that root. A fresh worker gets a fresh MCP process attached to the same project database. App Server owns process shutdown when the thread is retired.

## Allowlisted API

The server exposes one MCP tool named `memory`. Its `operation` field is a strict 14-branch JSON Schema union: `current`, `search`, `get`, `record_evidence`, `record_verified_fact`, `add_observation`, `propose_decision`, `set_decision_status`, `add_constraint`, `question`, `attach_evidence`, `conflict`, `user_correction`, and `milestone_evidence`.

Each operation rejects unknown or irrelevant arguments server-side. There is no raw SQL, shell execution, filesystem browsing, network, approval, process-control, or arbitrary tool proxy. MCP resources are empty.

Preflight queries `mcpServerStatus/list`, verifies the tool contract, then calls `mcpServer/tool/call` with `operation=current` to prove the server's project root. Before a model turn, the dispatcher repeats server status, contract, root, and initialization checks. A missing or disconnected server blocks the worker.

The plugin and every thread-level server definition set `approval_mode=prompt` for this tool. On first use in the initiating visible task, Codex offers `session` and `always`; the skill asks the user to choose `Always` for unattended fresh tasks. This is explicit user trust, not dispatcher auto-approval. App Server 0.153.4 exposes no read-only trust-status query, so preflight cannot independently prove which option the user chose. Production fails closed if a worker still requests approval.

If the MCP process fails during a turn, that turn may fail or be unable to satisfy the evidence gate. The dispatcher will not accept `ROTATE` or `DONE` without new evidence. Recovery uses a fresh worker/process; transparent mid-turn MCP process restart is not promised in this beta.
