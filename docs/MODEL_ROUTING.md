# Model routing

## Adaptive

Adaptive supports three strategies:

| Strategy | Code milestone | Computer Use milestone |
| --- | --- | --- |
| `auto` | GPT-5.6 Sol (`gpt-5.6-sol`) | GPT-6 Astra (`gpt-6-astra`) |
| `sol-only` | Sol | Blocks because the required capability is unavailable |
| `astra-only` | Astra | Astra |

The initiating skill classifies the Definition of Done and writes `execution_mode`, a concrete reason, and requested reasoning to `plan.json`. The deterministic dispatcher contains no classifier. Before every worker it checks `model/list`, maps the plan through the fixed registry, resolves supported effort, and records the exact selection in `run-state.json`.

Use `computer_use` only when completion requires real browser or desktop GUI interaction that code, files, shell tools, tests, builds, or programmatic interfaces cannot replace. Difficulty does not select Astra.

Public reasoning values are `medium`, `high`, `xhigh`, and `max`. If the requested level is unavailable, the dispatcher chooses the nearest advertised public level, preferring lower on an equal-distance tie. `ESCALATE` retries the same incomplete milestone in a fresh worker on the same model at the next public level. At `max`, it blocks. `REQUIRE_COMPUTER_USE` is AUTO-only: a Sol worker records a concrete GUI reason and a fresh Astra worker retries the same milestone without advancing the roadmap.

The dispatcher never changes model to evade quota. Sol and Astra share account allowance.

## When a newer model appears

The Adaptive profile asks App Server for an exact id - `gpt-5.6-sol`,
`gpt-6-astra` - and accepts nothing else. That strictness is deliberate: a
silent substitution would swap the declared capability rather than the
diligence, which is the one thing the hiring ladder is built not to do.

It has a consequence. A new model of the same family does not get used, and on
the day the pinned id stops being served, every installed copy refuses at the
same moment, wherever it is running.

So the catalog is compared rather than listed. `doctor` and preflight both read
what the account is actually served and answer one of three things per model:
it is served; it is served and something newer exists (named, with runs
continuing unchanged); or it is gone, and here is what is served instead. The
runtime never moves a pin by itself - naming an alternative is not taking it.

Moving to a newer model is an edit to `MODEL_IDS` in
`src/codex_autopilot/models.py` and a release. Host Settings is the standing
alternative for anyone who wants whatever the host currently applies: it sends
no model at all, and therefore no reasoning either, so the hiring ladder does
not apply to it.

## Host Settings

Host Settings plans omit reasoning. The dispatcher does not call `model/list` for routing and passes neither a `model` field to `thread/start` nor an `effort` field to `turn/start`. Each durable fresh task therefore receives whatever defaults the current App Server applies.

This proves omission, not a universal promise that a reasoning value selected in another Desktop task will be inherited. Host-default behavior is controlled by the current Codex/App Server build and remains a live compatibility item.
