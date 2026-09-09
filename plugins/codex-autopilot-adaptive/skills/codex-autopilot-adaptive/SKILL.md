---
name: codex-autopilot-adaptive
description: Run a long local Codex project as verified milestones in fresh visible workers. Use for requests to start, execute, pause, resume, inspect, or uninstall Codex Autopilot with deterministic Sol/Astra model routing, adaptive reasoning, and evidence-backed Project Memory.
---

# Codex Autopilot — Adaptive

Use this workflow only with an existing Git repository. The target repository may differ from the initiating task's working directory. The local dispatcher is deterministic and does not call a model while it waits, checks state, selects routes, or rotates workers.

## Autopilot worker

When the prompt identifies this task as a `Codex Autopilot v0.8 Desktop Native worker`, complete exactly its current milestone. Inspect the repository, evaluate every Definition of Done item, perform real verification, update the short `.codex-autopilot/HANDOFF.md`, and end with exactly one status line allowed by the worker prompt. Never create, fork, message, or start another Codex task. The dispatcher waits for `turn/completed` before it creates a fresh worker. Never bypass an approval or operate the Codex UI.

Project Memory is the canonical knowledge store. Use the built-in `memory` MCP tool and its allowlisted operations before relying on important historical claims. `HANDOFF.md`, model confidence, summaries, and agent prose are not evidence. Record hypotheses as Observations. Record Decisions and Constraints with explicit origin. A verified fact requires existing validated evidence: **NO EVIDENCE → NO TRUTH**. Before `ROTATE` or `DONE`, record new verification evidence linked to the current milestone. Do not edit `PROJECT_STATE.md` or `DECISIONS.md`; the dispatcher renders those human views from Project Memory.

Use Computer Use only when the worker prompt says `Effective execution mode: computer_use`. A code worker uses repository, shell, code, logs, and non-GUI tools even when the work is difficult. A computer-use worker performs the real GUI interaction required by the Definition of Done and never targets Codex itself.

In AUTO, a Sol code worker may discover that completion truly requires GUI interaction. Keep the milestone incomplete, update the handoff with the concrete GUI requirement, add exactly one `COMPUTER_USE_REASON: <specific reason>` line, and finish with `AUTOPILOT_STATUS: REQUIRE_COMPUTER_USE`. Complexity is not a valid reason.

## Start a run

Resolve the user's target Git repository explicitly, even when it is outside this task's current directory. Inspect it and the user's goal or `ROADMAP.md`. Select one model strategy for the run:

- `auto` for `Use Codex Autopilot for this project` and requests without an override.
- `sol-only` for `Use Codex Autopilot with Sol only for this project`.
- `astra-only` for `Use Codex Autopilot with Astra only for this project`.

Before creating the plan, call the built-in `memory` tool once with `operation=current`. This is the first-use trust probe; an `initialized: false` response is normal when the initiating task is outside an initialized Autopilot project. If Codex asks whether to allow this local tool, explain that the one tool is bundled with Autopilot, project-scoped, has no shell or network access, and covers all memory operations. The user must choose Codex's `Always` option for unattended fresh workers. Never answer the approval on the user's behalf, never change MCP approval configuration, and do not start a run if the user declines. A session-only choice can protect the initiating task but will not cover fresh worker tasks.

Create one independently verifiable outcome per milestone. For every milestone ask whether its Definition of Done requires Computer Use. Use `computer_use` only for required browser or desktop GUI interaction that files, code, shell tools, or programmatic interfaces cannot replace. Coding, architecture, debugging, networking, tests, Git, builds, HTML/CSS, documentation, and file-based asset edits remain `code` regardless of difficulty.

Assign reasoning independently: `medium` for routine execution, `high` for difficult implementation or debugging, `xhigh` for cross-system root-cause work, and `max` only for rare foundational architecture or research. Duration alone does not raise reasoning. Model choice never depends on reasoning complexity.

Write `<target-root>/.codex-autopilot/bootstrap-plan.json`:

```json
{"goal":"...","model_strategy":"auto","milestones":[{"title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient.","reasoning":"medium"}]}
```

`execution_mode_reason` must state the concrete capability boundary. For `computer_use`, name the GUI application or browser interaction required.

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run:

```text
scripts/codex-autopilot start-skill --project <target-root> --plan-file <target-root>/.codex-autopilot/bootstrap-plan.json --replace
```

This command performs preflight before it creates run-state. It checks the target, Git, installed runtime, official Codex App Server, `:workspace`, target cwd, built-in Project Memory MCP, SQLite FTS5, and model metadata. Its `Memory tool trust` line is an explicit reminder; App Server exposes no read-only API for preflight to inspect the user's persistent per-tool choice. If it exits with code 77 and says `APPROVAL REQUIRED`, do not initialize or imitate a launch. Use Codex's normal permission mechanism to request read/write access to the exact reported `CODEX_HOME` directory for the official App Server process, then rerun the same command. Never request global/full access.

Show the user the short milestone list and selected routes, then finish the initiating turn. The trusted Stop hook claims the explicit target from the short-lived launch registry, starts the detached non-AI dispatcher, and passes the initiating thread/turn IDs. Worker 1 waits for durable completion of this turn. Do not wait for a worker and do not act as a controller.

If the target is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Controls

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill, run the bundled helper with `stop`, `resume`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

## Status protocol

- `ROTATE`: current milestone verified with recorded evidence; another milestone remains.
- `DONE`: the complete roadmap is verified with recorded evidence.
- `BLOCKED`: user input, an unavailable required model, or an approval is required.
- `ESCALATE`: retry the same incomplete milestone in a fresh worker on the same model at the next reasoning level. At `max`, the dispatcher records `BLOCKED`.
- `REQUIRE_COMPUTER_USE`: AUTO-only Sol→Astra capability escalation for the same incomplete milestone. It never advances the roadmap.
