# Architecture

Codex Autopilot consists of a Codex plugin and a local Python dispatcher. Python lives in Application Support and is not the normal user interface.

The initiating model creates the structured plan and runs the bundled `start-skill` helper. A trusted synchronous Stop hook starts a detached dispatcher. That dispatcher records the initiating thread and turn IDs, polls `thread/read`, and keeps the Worker 1 gate closed until the initiating turn is durably `completed`.

For every milestone, the Adaptive dispatcher reads `plan.json`, maps its strategy and effective execution mode to Sol or Astra, and validates that exact ID and requested effort through `model/list`. Host Settings skips model metadata and sends no model or effort override. The dispatcher then calls `thread/start` with project cwd, `ephemeral: false`, `permissions: :workspace`, optional Saved Project ID, and the selected model ID only in Adaptive. It names the thread and calls `turn/start` with text, the installed skill, and Adaptive effort.

After `turn/completed`, the dispatcher validates the status and compact checkpoint. `ROTATE` advances the plan. `ESCALATE` retains the model capability and raises reasoning in a fresh thread. AUTO-only `REQUIRE_COMPUTER_USE` retains the milestone index and starts a fresh Astra thread after a Sol worker records a concrete reason. `DONE` and `BLOCKED` terminate the loop. A file lock prevents two dispatchers for one project.

Persistent files:

- `ROADMAP.md`: human-readable plan and deterministic completion marks.
- `.codex-autopilot/plan.json`: strategy, execution modes, reasons, milestones, and Adaptive reasoning source.
- `MILESTONE.md`: current milestone selected by deterministic code.
- `PROJECT_STATE.md`: verified repository state, replaced by every worker.
- `HANDOFF.md`: minimal context for the next fresh worker, replaced by every worker.
- `DECISIONS.md`: durable constraints only.
- `run-state.json`: atomic dispatcher journal, current route, and per-worker history.

Each history record stores milestone ID, planned/effective execution mode, strategy, model ID, reasoning, selection reason, thread ID, turn ID, status, and timestamps.

There is no controller model. The dispatcher performs JSON-RPC, catalog validation, file validation, retry timing, state transitions, and process control without model allowance.
