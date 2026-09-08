---
name: codex-autopilot-adaptive
description: Run a long local Codex project as verified milestones in fresh visible workers. Use for requests to start, execute, pause, resume, inspect, or uninstall Codex Autopilot with deterministic Sol/Astra model routing and per-milestone reasoning.
---

# Codex Autopilot — Adaptive

Use this workflow only in an existing Git repository. The local dispatcher is deterministic and does not call a model while it waits, selects models, or rotates workers.

## Dispatcher worker

When the prompt identifies this task as a `Codex Autopilot v0.7 Desktop Native worker`, follow its selected model, effective execution mode, and current milestone. Work on exactly that milestone. Inspect the actual repository, meet and verify its Definition of Done, replace `.codex-autopilot/PROJECT_STATE.md` and `.codex-autopilot/HANDOFF.md`, and update `.codex-autopilot/DECISIONS.md` only for durable decisions. End with exactly one status line allowed by the worker prompt. Never create or contact another task; the dispatcher acts only after `turn/completed`. Never bypass an approval or operate the Codex UI.

Use Computer Use only when the worker prompt says `Effective execution mode: computer_use`. A code worker uses repository, shell, code, logs, and non-GUI tools even when the task is difficult. A computer-use worker must perform the real GUI interaction required by the Definition of Done and may never target Codex itself.

In AUTO, a Sol code worker may discover that the Definition of Done truly requires real GUI interaction. Then keep the milestone incomplete, write the concrete GUI requirement into the handoff, add exactly one `COMPUTER_USE_REASON: <specific reason>` line, and finish with `AUTOPILOT_STATUS: REQUIRE_COMPUTER_USE`. Complexity or a desire for a stronger model is never a valid reason.

## Start a new run

For a start request, inspect the repository and the user's goal or `ROADMAP.md`. Select one model strategy for the entire run:

- `auto` for `Use Codex Autopilot for this project` and any request without an override.
- `sol-only` for `Use Codex Autopilot with Sol only for this project`.
- `astra-only` for `Use Codex Autopilot with Astra only for this project`.

Create a concise plan with one independently verifiable outcome per milestone. For every milestone ask: **Does its Definition of Done require Computer Use?** Use `computer_use` only for real browser or desktop GUI interaction that cannot be reliably replaced with files, code, shell tools, or programmatic interfaces. Unreal Editor, Blender, cross-app GUI work, and required visual GUI verification qualify. Coding, architecture, debugging, networking, tests, Git, builds, HTML/CSS, documentation, and file-based asset edits remain `code`, regardless of complexity.

Assign reasoning independently: `medium` for routine execution, `high` for difficult implementation or debugging, `xhigh` for cross-system root-cause work, and `max` only for rare foundational architecture or research. Duration alone does not raise the level. Model choice never depends on reasoning complexity.

Write `.codex-autopilot/bootstrap-plan.json` in this shape:

```json
{"goal":"...","model_strategy":"auto","milestones":[{"title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient.","reasoning":"medium"}]}
```

`execution_mode_reason` must state the concrete DoD capability boundary. For `computer_use`, name the GUI application or browser interaction required.

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run:

```text
scripts/codex-autopilot start-skill --project <project-root> --plan-file <project-root>/.codex-autopilot/bootstrap-plan.json --replace
```

Show the user the short milestone list, model strategy, execution modes, and reasoning recommendations, then finish the initiating turn. The trusted Stop hook confirms the detached dispatcher is alive; Worker 1 waits until this initiating turn is completed. Do not wait for a worker and do not act as a controller.

If the project is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Control

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill anyway, run the bundled helper with `stop`, `resume`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

## Status protocol

- `ROTATE`: current milestone verified; another planned milestone remains.
- `DONE`: the complete roadmap is verified.
- `BLOCKED`: user input, an unavailable required model, or an unavailable approval is required.
- `ESCALATE`: retry the same unfinished milestone in a fresh worker on the same model capability at the next reasoning level. At `max`, the dispatcher records `BLOCKED`.
- `REQUIRE_COMPUTER_USE`: AUTO-only Sol→Astra capability escalation for the same unfinished milestone. It never advances the roadmap.
