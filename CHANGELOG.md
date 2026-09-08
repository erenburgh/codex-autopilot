# Changelog

## 0.7.0-beta

- Added deterministic AUTO routing: code milestones use GPT-5.6 Sol and Computer Use milestones use GPT-6 Astra.
- Added `auto`, `sol-only`, and `astra-only` strategies plus explicit execution modes in `plan.json`.
- Added App Server model metadata validation, supported-effort resolution, worker routing history, and visible model status.
- Added AUTO-only `REQUIRE_COMPUTER_USE` escalation from a fresh Sol worker to a fresh Astra worker on the same milestone.
- Kept Host Settings free of model and effort overrides and kept rate limits account-wide without model fallback.

## 0.6.0-beta

- Renamed the model-neutral App Server core to Codex Autopilot.
- Made installed Codex skills and trusted lifecycle hooks the normal entry point.
- Added strict initiating-turn completion gating, serial visible workers, compact state, `PAUSED` recovery, and exact no-model controls.
- Unified Adaptive reasoning around `plan.json` and removed unsupported values and hidden overrides.
- Added a Host Settings profile that omits `effort`.
- Removed preview runners, compatibility schemas, headless execution, UI automation, and project-specific fixtures from production.
- Added a one-action macOS bootstrap installer, legacy preview backup, uninstall, and clean user release package.
