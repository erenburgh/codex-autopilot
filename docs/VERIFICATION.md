# Verification status

Validation target: macOS 26.6.2 arm64, Codex CLI/App Server 0.153.4, ChatGPT Desktop 26.901.51231, Python 3.14.7, GPT-5.6 Sol, and GPT-6 Astra.

## Model metadata

Live App Server `model/list` returned:

- `gpt-5.6-sol`: low, medium, high, xhigh, max, ultra.
- `gpt-6-astra`: low, medium, high, xhigh, max, ultra.

The public Adaptive contract uses medium, high, xhigh, and max. The dispatcher verifies the current catalog before every Adaptive worker.

## Automated suite

`PYTHONPATH=src python3 -m unittest discover -s tests -v` passes 45 tests. These cover all 15 requested routing and lifecycle checks, App Server request shape, install/reinstall, recovery gates, approval interruption, contamination rules, and release invariants.

## Live acceptance

All runs used real App Server threads and recorded exact model, effort, thread, turn, status, and MCP item metadata.

| Scenario | Observed route | Result |
| --- | --- | --- |
| Sol-only three-worker flow | Sol M1 `ROTATE` → Sol M2 `ROTATE` → Sol M3 `DONE` | PASS |
| AUTO mixed flow | Sol/code `ROTATE` → Astra/computer_use `ROTATE` → Sol/code `DONE` | PASS |
| AUTO capability escalation | Sol/code `REQUIRE_COMPUTER_USE` → fresh Astra/computer_use on the same M1 → `DONE` | PASS |

Each run used distinct durable thread IDs and the App Server log showed no overlapping `turn/started` intervals. In the mixed and escalation runs, only the Astra worker emitted `mcpToolCall` items for `cua_repl`. Both Astra workers called `cua.createBrowserTab("iab", "https://example.com", {visible:true})` and then `getScreenshot()` to verify the visible `Example Domain` heading. Sol workers emitted no Computer Use call.

The browser live tests required two developer acceptance actions that production never performs: the visible Astra task was manually foregrounded before `turn/start`, and `scripts/live_acceptance.py --approve-live-test-surface` answered only the exact `https://example.com` origin request for that test session. Without an available browser/app permission, the production dispatcher fails closed, interrupts the turn, and records `BLOCKED`.

## Known beta limits

- App Server is experimental.
- On ChatGPT Desktop 26.901.51231, an App Server-created task's in-app browser was unavailable until the task was foreground.
- Production does not broker interactive approval requests; a Computer Use milestone that lacks an already-available permission becomes `BLOCKED`.
- Saved Project placement is not live-verified.
- Host Settings cannot promise inheritance of another task's UI-selected model or reasoning; it only proves that model and effort fields are omitted.
- Continuation after waiting through a real multi-hour reset is not live-observed; v0.6 did observe the real five-hour error and reset metadata.
- Reboot recovery requires an explicit resume.
