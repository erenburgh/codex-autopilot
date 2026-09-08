# Codex Autopilot

Run long Codex projects as serial fresh workers with compact handoffs and deterministic Sol/Astra model routing.

Codex Autopilot prevents context decay by assigning one milestone to one durable, visible Codex thread. A small non-AI dispatcher waits for `turn/completed`, validates the checkpoint, and then starts the next worker. There is no controller model and never more than one active Autopilot model turn.

## Smart model routing

Adaptive uses `auto` by default:

- GPT-5.6 Sol handles regular coding, architecture, debugging, analysis, builds, tests, and other work that does not require Computer Use.
- GPT-6 Astra handles only milestones whose Definition of Done requires real browser or desktop GUI interaction through Computer Use.

Complexity does not select Astra. Model capability and reasoning effort are independent axes. Sol and Astra use the same account allowance; Codex Autopilot does not treat them as separate quotas and never changes model to evade a rate limit.

The initiating planner records `execution_mode: code|computer_use` and a concrete reason in `.codex-autopilot/plan.json`. Before every worker the dispatcher resolves that mode through the selected strategy, validates the exact model and supported effort against App Server `model/list`, and records the result in `run-state.json`.

## Install

Codex Autopilot currently supports macOS.

Clone the repository:

```bash
git clone https://github.com/erenburgh/codex-autopilot.git
cd codex-autopilot
./install.sh --profile adaptive --install-deps
```

Alternatively, download and extract the macOS release ZIP and run:

```bash
./install.sh --profile adaptive --install-deps
```

Open `/hooks` in Codex and trust the two Codex Autopilot hook commands once. Use `--profile host-settings` when every fresh worker should receive the host defaults with no model or effort override.

## Use

Open an existing Git repository in Codex and write:

> Use Codex Autopilot for this project.
>
> Goal: Build the complete inventory system.
>
> Break it into milestones and continue until DONE.

This selects AUTO. The explicit alternatives are:

- `Use Codex Autopilot with Sol only for this project.`
- `Use Codex Autopilot with Astra only for this project.`

The initiating turn creates the plan and arms the trusted Stop hook. The dispatcher waits until that turn is durably `completed`, then creates Worker 1. Every later worker starts only after the previous worker's `turn/completed`.

Exact no-model controls:

- `Pause Codex Autopilot.`
- `Resume Codex Autopilot.`
- `What is Codex Autopilot doing right now?`
- `Uninstall Codex Autopilot.`

## Capability escalation

In AUTO, a Sol worker that proves the current Definition of Done needs real GUI interaction may return `REQUIRE_COMPUTER_USE` with a concrete reason. The roadmap does not advance. The dispatcher creates a fresh Astra worker for the same milestone and retains the independently selected reasoning effort. There is no Astra-to-Sol restart.

`ESCALATE` remains reasoning-only: a fresh worker uses the same model capability at the next public level, `medium → high → xhigh → max`.

## Safety and limits

Workers use App Server `:workspace`. The dispatcher does not answer approvals, modify global Codex settings, change Git configuration, grant macOS permissions, or auto-commit by default. Model IDs are selected per new thread; no account default is changed.

The public beta supports macOS and is verified against Codex CLI/App Server 0.153.4. App Server is experimental. On the tested Desktop build, an App Server-created task had to be foreground before its in-app browser surface became available. The production dispatcher never answers a Computer Use or browser approval request, so a GUI milestone becomes `BLOCKED` when the required permission is not already available.

Saved Project routing and continuation through a multi-hour rate reset remain beta-limited; resume after reboot is manual.

See [Getting Started](GETTING_STARTED.md), [Model Routing](docs/MODEL_ROUTING.md), [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md), and [Verification](docs/VERIFICATION.md).
