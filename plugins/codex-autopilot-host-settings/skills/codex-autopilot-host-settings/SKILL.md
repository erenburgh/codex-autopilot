---
name: codex-autopilot-host-settings
description: Run a long local Codex project as verified milestones in fresh visible workers with evidence-backed Project Memory while leaving model and reasoning selection to host defaults.
---

# Codex Autopilot — Host Settings

Use this workflow only with an existing Git repository. The target repository may differ from the initiating task's working directory. The local dispatcher is deterministic and does not call a model while it waits or rotates workers. This profile performs no Sol/Astra routing and sends neither a model nor a reasoning override.

## Autopilot worker

When the prompt identifies this task as a `Codex Autopilot v0.8 Desktop Native worker`, complete exactly its current milestone. Inspect the repository, evaluate every Definition of Done item, perform real verification, update the short `.codex-autopilot/HANDOFF.md`, and end with exactly one allowed status line. Never create, fork, message, or start another Codex task. The dispatcher waits for `turn/completed` before it creates a fresh worker. Never bypass an approval or operate the Codex UI.

Project Memory is the canonical knowledge store. Use the built-in `memory` MCP tool and its allowlisted operations before relying on important historical claims. `HANDOFF.md`, model confidence, summaries, and agent prose are not evidence. Record hypotheses as Observations and keep Decisions, Constraints, and verified facts distinct. A verified fact requires existing validated evidence: **NO EVIDENCE → NO TRUTH**. Before `ROTATE` or `DONE`, record new verification evidence linked to the current milestone. Do not edit `PROJECT_STATE.md` or `DECISIONS.md`; the dispatcher generates those human views.

The execution mode documents what the Definition of Done requires, but this profile does not select a capability. Use Computer Use only when the current host-selected model provides it and the prompt says `Effective execution mode: computer_use`. Otherwise return `BLOCKED` if the Definition of Done cannot be completed.

## Start a run

Resolve the user's target Git repository explicitly, even when it is outside this task's current directory. Inspect it and the user's goal or `ROADMAP.md`. Create one independently verifiable outcome per milestone. Set `model_strategy` to `host-settings`. Do not add a reasoning field: the dispatcher omits both model and effort, and each fresh thread uses whatever defaults the host applies.

Before creating the plan, call the built-in `memory` tool once with `operation=current`. This is the first-use trust probe; `initialized: false` is normal outside an initialized Autopilot project. If Codex asks whether to allow it, explain that this one bundled local tool is project-scoped, has no shell or network access, and covers all memory operations. The user must choose Codex's `Always` option for unattended fresh workers. Never answer for the user, change MCP approval configuration, or start after a decline. A session-only choice does not cover fresh worker tasks.

Classify each milestone as `code` or `computer_use` based only on whether its Definition of Done requires real GUI interaction that files, code, shell tools, or programmatic interfaces cannot replace. Include a concrete `execution_mode_reason`.

Write `<target-root>/.codex-autopilot/bootstrap-plan.json`:

```json
{"goal":"...","model_strategy":"host-settings","milestones":[{"title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient."}]}
```

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run:

```text
scripts/codex-autopilot start-skill --project <target-root> --plan-file <target-root>/.codex-autopilot/bootstrap-plan.json --replace
```

This command performs preflight before it creates run-state. It checks the target, Git, installed runtime, official Codex App Server, `:workspace`, target cwd, built-in Project Memory MCP, and SQLite FTS5. Its `Memory tool trust` line is an explicit reminder; App Server exposes no read-only API for preflight to inspect the persistent per-tool choice. If it exits with code 77 and says `APPROVAL REQUIRED`, use Codex's normal permission mechanism to request read/write access to the exact reported `CODEX_HOME` directory for the official App Server process, then rerun. Never request global/full access.

Show the short milestone list, then finish the initiating turn. The trusted Stop hook claims the explicit target from the short-lived launch registry and starts the detached non-AI dispatcher. Worker 1 waits for durable completion of this turn. Do not wait for a worker and do not act as a controller.

If the target is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Controls

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill, run the bundled helper with `stop`, `resume`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

## Status protocol

- `ROTATE`: current milestone verified with recorded evidence; another milestone remains.
- `DONE`: the complete roadmap is verified with recorded evidence.
- `BLOCKED`: user input, a missing host capability, or an approval is required.
