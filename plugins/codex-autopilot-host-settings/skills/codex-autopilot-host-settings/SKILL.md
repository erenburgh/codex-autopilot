---
name: codex-autopilot-host-settings
description: Run a long local Codex project as verified milestones in fresh visible workers while leaving both model and reasoning selection to the defaults the host applies to each new thread.
---

# Codex Autopilot — Host Settings

Use this workflow only in an existing Git repository. The local dispatcher is deterministic and does not call a model while it waits or rotates workers. This profile performs no Sol/Astra routing.

## Dispatcher worker

When the prompt identifies this task as a `Codex Autopilot v0.7 Desktop Native worker`, follow that prompt. Work on exactly the current milestone. Inspect the actual repository, meet and verify its Definition of Done, replace `.codex-autopilot/PROJECT_STATE.md` and `.codex-autopilot/HANDOFF.md`, and update `.codex-autopilot/DECISIONS.md` only for durable decisions. End with exactly one allowed status line. Never create or contact another task; the dispatcher acts only after `turn/completed`. Never bypass an approval or operate the Codex UI.

The recorded execution mode documents what the Definition of Done needs, but this profile does not enforce a model capability. Use Computer Use only when the current host-selected model provides it and the worker prompt says `Effective execution mode: computer_use`. Otherwise return `BLOCKED` if the Definition of Done cannot be completed.

## Start a new run

For a start request, inspect the repository and the user's goal or `ROADMAP.md`. Create a concise milestone plan with one independently verifiable outcome per milestone. Set `model_strategy` to `host-settings`. Do not add a reasoning field: the dispatcher omits both model and effort, and each fresh thread uses whatever defaults the host applies to it.

Classify each milestone as `code` or `computer_use` by asking whether its Definition of Done requires real GUI interaction that files, code, shell tools, or programmatic interfaces cannot replace. Complexity is unrelated to this label. Include a concrete `execution_mode_reason`.

Write `.codex-autopilot/bootstrap-plan.json` in this shape:

```json
{"goal":"...","model_strategy":"host-settings","milestones":[{"title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient."}]}
```

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run:

```text
scripts/codex-autopilot start-skill --project <project-root> --plan-file <project-root>/.codex-autopilot/bootstrap-plan.json --replace
```

Show the user the short milestone list and finish the initiating turn. The trusted Stop hook confirms the detached dispatcher is alive; Worker 1 waits until this initiating turn is completed. Do not wait for a worker and do not act as a controller.

If the project is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Control

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill anyway, run the bundled helper with `stop`, `resume`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

## Status protocol

- `ROTATE`: current milestone verified; another planned milestone remains.
- `DONE`: the complete roadmap is verified.
- `BLOCKED`: user input, a missing host capability, or an unavailable approval is required.
