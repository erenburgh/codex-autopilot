# Model routing

## Contract

Codex Autopilot v0.7 routes between exactly two models in the Adaptive profile:

| Execution mode | AUTO model |
| --- | --- |
| `code` | GPT-5.6 Sol (`gpt-5.6-sol`) |
| `computer_use` | GPT-6 Astra (`gpt-6-astra`) |

The planner records `model_strategy`, `execution_mode`, `execution_mode_reason`, and Adaptive `reasoning` in `plan.json`. The dispatcher contains no inference: it maps these fields to a model, validates the current App Server catalog, resolves a supported effort, and calls `thread/start` and `turn/start`.

## Classifying milestones

The deciding question is: **Does the Definition of Done require Computer Use?**

Use `computer_use` for required interaction with a real browser or professional desktop GUI, including Unreal Editor, Blender, cross-app workflows, and visual GUI verification that cannot be replaced reliably by code or files.

Use `code` for programming, architecture, debugging from code or logs, networking, refactoring, tests, HTML/CSS, documentation, Git, builds, and programmatic file or asset changes. Difficulty and duration never select Astra.

`execution_mode_reason` must explain the concrete boundary. “This is hard” is invalid.

## Strategies

- `auto`: code → Sol; computer_use → Astra.
- `sol-only`: code → Sol; computer_use → `BLOCKED` with a clear capability error.
- `astra-only`: every milestone → Astra.
- `host-settings`: available only in the Host Settings profile; the dispatcher sends neither model nor effort.

Missing or unavailable models produce `BLOCKED`. AUTO never silently replaces unavailable Sol with Astra, and it never replaces unavailable Astra with Sol.

## Reasoning is independent

The public plan values are `medium`, `high`, `xhigh`, and `max`. Before each worker the dispatcher reads `model/list`. If the requested effort is absent, it selects the nearest advertised public level, preferring the lower level on an equal-distance tie, logs the adjustment, and stores it in worker history. If no public level is supported, the run becomes `BLOCKED`.

On Codex App Server 0.153.4, both `gpt-5.6-sol` and `gpt-6-astra` advertised `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`. The Autopilot public contract deliberately uses only `medium`, `high`, `xhigh`, and `max`; user terminology `ultra` normalizes to `max`.

`ESCALATE` creates a fresh worker at the next public reasoning level while preserving the model capability selected for the milestone.

## Capability escalation

`REQUIRE_COMPUTER_USE` is available only to a Sol code worker in AUTO. The worker must record a specific `COMPUTER_USE_REASON`. The dispatcher keeps the roadmap index unchanged and starts a fresh Astra worker for the same milestone. The reasoning layer remains independent and no Astra-to-Sol restart occurs.

## Shared allowance

Sol and Astra use the same account allowance. Rate limiting is account-wide. The dispatcher waits for the shared reset and retries the same route; it never changes models to work around quota.

Availability is separate from quota. The required ID must appear in `model/list`, and `thread/start` must confirm the requested model. Failure becomes `BLOCKED`; there is no hidden fallback.
