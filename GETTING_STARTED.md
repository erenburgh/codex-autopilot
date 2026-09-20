# Getting Started

## Requirements

- macOS.
- Codex Desktop.
- The official Codex CLI, signed in, with `codex app-server` available.
- An eligible ChatGPT/Codex account.
- Python 3.11 or newer.
- An existing Git repository, **with at least one commit**, for the project Autopilot will change.

Normal use does not require Python commands, pip, a manual venv, PATH changes, or manual state editing. Homebrew is optional.

Git is local here and unrelated to GitHub: no remote, nothing pushed, and Autopilot creates no commits of its own (`git.auto_commit` is off). It is there because it is how the runtime sees what a task changed - `git diff --name-only` plus `git ls-files --others` - and that observation is what the declared write scope is audited against. Autopilot does not run `git init` for you.

One commit matters. A repository that has been initialised but never committed has no `HEAD`, so there is nothing to compare against: the run works, but the write-scope rule is recorded as unchecked for every task instead of being enforced. Preflight prints `Git: WARN` in that case and says so.

## Install once

Clone the repository or extract the macOS release ZIP, then run:

```bash
./install.sh --profile adaptive --install-deps
```

Use `--profile host-settings` if every fresh worker should use App Server host defaults with no model or effort override.

The installer:

1. checks macOS, Python 3.11+, `codex app-server`, and Codex login;
2. optionally installs missing Python through an existing Homebrew and missing Codex CLI through npm;
3. creates `~/Library/Application Support/CodexAutopilot/<version>/` with a pip-free venv, the runtime tree (sources, the test suite, docs, the installer), and both profile bundles;
4. replaces the memory MCP and lifecycle-hook launcher placeholders with the absolute, stable `current/bin/codex-autopilot` runtime path;
5. updates the `current` symlink;
6. registers the local marketplace and exactly one selected profile through `codex plugin`;
7. writes the wake-up launch agent `~/Library/LaunchAgents/com.codex-autopilot.wake.plist` and loads it with `launchctl`;
8. adds a marked block to the Codex execpolicy (`$CODEX_HOME/rules/default.rules`, normally `~/.codex/rules/default.rules`) allowing its own installed script to be launched with `start-skill` and `timeline`. Without it Codex raises an approval dialog on launch, the dispatcher never answers approvals by rule, and the run hangs in a task nobody is looking at. Nothing else is allowed by that block, rules you or Codex wrote are left untouched, and `uninstall --yes` removes it.

That agent is what the installer leaves running on your Mac. It starts `codex-autopilot _wake-sweep` at login and again every 300 seconds, for as long as it stays installed, and writes what it decided to `~/Library/Application Support/CodexAutopilot/wake-sweep.log`. A sweep looks only at the projects Autopilot has already armed: where a task waits for a rate-limit retry whose time has come and no wake-up is waiting for it, the sweep arms one, and otherwise it does nothing. It starts no worker itself and does not wake a run you paused or stopped. Install with `CODEX_AUTOPILOT_SKIP_LAUNCHD=1 ./install.sh --profile adaptive` to write the agent without loading it; `codex-autopilot uninstall --yes` boots it out and deletes it.

Previous installations are no longer kept beside the new one: each older version directory is zipped into `~/Library/Application Support/CodexAutopilot/legacy-backups/previous-installs-<stamp>.zip` and then removed, and the installer prints how many and where. One exception: an installation carrying accepted runtime repairs is renamed to `<version>.repaired-<stamp>` beside itself and kept.

Apart from that launch agent and that execpolicy block, the installer does not modify PATH, shell profiles, Git config, repository history, global Codex model/reasoning/sandbox/network settings, other macOS settings, or unrelated plugins. The complete list of what installation and uninstallation touch is in [Install and uninstall footprint](docs/INSTALL_FOOTPRINT.md).

Open `/hooks` in Codex and trust the current Autopilot **Stop** hook once. Hook trust is a normal Codex security step and the installer cannot bypass it. The installed hook command points to the permanent `current/bin/codex-autopilot` entrypoint rather than a version or plugin-cache directory, so ordinary upgrades and cachebuster reinstallations preserve its command identity. Codex asks again only after a real hook-definition change.

Start a fresh Codex task after installation. On the first Autopilot request, `start-skill` creates a dedicated preflight task and makes a harmless real model-to-MCP call with `operation=current`. If Codex requires approval, the command stops before run-state or Worker 1 and the initiating task asks whether you approve **Always** for the single bundled memory tool. Only after an explicit yes may it repeat the command with `--approve-project-memory-always`; that flag answers the exact pending App Server request and cannot bypass a different approval. Project initialization and Worker 1 begin only after a subsequent fresh-task probe completes without interruption.

## Install by asking

Open the Codex project you want to work on and say:

```text
Download and install this skill, then start working on this project with it:
https://github.com/erenburgh/codex-autopilot
```

Codex clones the repository and runs `./install.sh` itself. You do not choose a
directory: the project you are in is the target, because every task Autopilot
creates is placed in that project and verified there. A directory that belongs
to no Codex project is refused before anything is created, in one sentence that
names what to do.

Two Codex trust decisions remain, and they are Codex's, not Autopilot's: trust
the Stop hook once in `/hooks`, and approve the bundled `memory` tool with
`Always` when the preflight task asks. Both are requested before the first
milestone starts, never in the middle of one.

## Start

In any Codex task, name the target repository explicitly:

```text
Use Codex Autopilot for /absolute/path/to/project.

Goal:
Build the complete inventory system.

Break it into independently verifiable milestones and continue autonomously until DONE.
```

The initiating model inspects the target and writes a small bootstrap plan. It resolves the intended saved Desktop `projectId` but does not pre-create worker slots. You do not write `ROADMAP.md` by hand. The bundled `start-skill` helper validates the plan and runs bounded preflight, then prints results such as:

The initiating request's language is stored as a BCP-47 tag (for example `ru` or `en`) and inherited by every fresh worker. The generated plan and reservation prompts use the same language. Worker commentary and final reports must use it even when older source material is in another language; `AUTOPILOT_STATUS`, `AUTOPILOT_SLOT_READY`, code, identifiers, file names, and tool names stay exact.

```text
Target: OK
Git: OK
Runtime: OK
App Server: OK
Autopilot Stop hook: OK
Codex project metadata: OK
Desktop UI placement: OK (or TASKS/RECENTS when the target is outside saved Project roots)
Worker access: OK
Project Memory transport: OK
Project Memory MCP: OK
Model metadata: OK
Preflight: PASS
```

Only after PASS and full preflight App Server process exit does initialization
write `prep_app_server_exited_at` and arm a launch. The production gate uses the
supported `hooks/list` App Server method and requires exactly one enabled Stop
hook for the selected Autopilot plugin, with `trustStatus` equal to `trusted` or
`managed` and the exact stable installed-runtime command. `modified` or
`untrusted` exits 77 with `APPROVAL REQUIRED`; missing, disabled, duplicated,
erroneous, or mismatched hook inventory fails. The same gate runs immediately
before each READY reservation. Hook trust permits local lifecycle mutation only;
the reservation records causality and is never treated as transport authority.

The trusted Stop hook atomically reserves the bounded READY frontier, records
its own `session_id` as causal provenance, and launches the local fixed relay
for the exact durable reservation. It does not ask a model to choose work or
return an actionable continuation. The relay performs the already-authorized
App Server create/start/wait sequence and exits. Unknown outcomes remain
`AMBIGUOUS` and are never retried blindly. Pipeline Engineer may repair the
transport and re-arm the same causal predecessor, but it never launches the
destination task itself. See [Pipeline Engineer · On call](docs/PIPELINE_ENGINEER.md).

Preflight confirms transport, tool schema, FTS5, target binding, App Server project metadata, and a real model-to-MCP call. Current App Server has no read-only method that reveals persistent `Always` state, so the harmless call is the supported behavioral probe. If a selected-plugin request advertises `always` in its persistence metadata, preflight exits with code 77 and leaves the run uninitialized. The initiating task must ask the user; after explicit approval it repeats the command with `--approve-project-memory-always`, which sends `accept` plus Codex's advertised persistent metadata only to that verified memory request. Autopilot refuses the flag when Codex does not advertise `always`. A final unassisted probe must then pass before Worker 1 is created.

Worker filesystem scope and Desktop placement are separate. The exact causal predecessor's Stop hook launches a detached dispatcher. The dispatcher calls App Server `thread/start` with the canonical target cwd and App Server project ID, verifies title/cwd/project metadata, calls production `turn/start`, waits for completion, and exits. It never calls Codex App `create_thread` or `send_message_to_thread`, and hook feedback is not used to continue a model turn. App Server and Desktop project IDs are different namespaces; actual Desktop visibility/editability is checked independently. `thread/unsubscribe` removes only one connection's subscription and is not treated as an ownership handoff.

### First-run access

The official App Server stores its state, SQLite WAL/SHM files, locks, plugin cache, and temporary wrappers under `CODEX_HOME` (normally `~/.codex`). In a restricted initiating task, macOS/Codex may block that child process. Preflight then prints `Worker access: APPROVAL REQUIRED`, names the exact directory, exits with code 77, and leaves the target uninitialized. Approve read/write access to that exact directory through Codex's normal permission UI, then repeat the same start request. Autopilot does not inspect App Server databases and does not request broad disk access.

## What runs

Up to `max_parallel_workers` independent, resource-compatible READY tasks may overlap. Each worker completes one task, records evidence through the local memory MCP, writes a short handoff, and returns an allowed status. The Desktop Stop hook supplies authoritative task/turn identity before dependencies advance. Conflicting work waits; retries affect only their own task.

## Skills per task

Before a task is given a worker, a short screening session decides which skills
that worker should carry. It reads the task, the skills already installed in
your Codex home, and the governed packs this project declares. A bundle hired
on an earlier run is not offered back to it yet, so the same capability can be
asked for twice. It may also name a
skill it does not have yet, as a public repository plus a path inside it; the
runtime fetches that bundle itself - the screening session never reaches the
network - and copies it into `.codex-autopilot/hired-skills/` inside the
project. Nothing goes to `~/.codex/skills`, the Codex plugin cache, hooks, or
MCP configuration, and a fetch that fails is recorded as an unmet need rather
than a failed task.

This is on by default and costs one extra Codex thread per task out of your
limits. `status` shows what it spent and what it bought. To change it:

```toml
# .codex-autopilot/config.toml
[runtime]
skill_screening = "always"          # default; "auto" screens only when the plan
                                    # or this project's pack library declares a
                                    # pack - it does not look at your installed
                                    # skills; "never" switches it off
skill_fetch_hosts = ["github.com", "raw.githubusercontent.com"]
```

An empty `skill_fetch_hosts` list means nothing is ever downloaded: a named
skill is then recorded as an unmet need. To see or remove what a project holds:

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" skills --project /absolute/path/to/project
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" revoke-skill --project /absolute/path/to/project --skill-id <id>
```

See [skill screening](docs/SKILL_SCREENING.md).

## Pause, resume, and inspect

- Send `Pause Codex Autopilot.` to stop new launches. Existing workers drain
  deterministically and retain their locks until an authoritative Stop or
  Interrupt is recorded.
- Send `Resume Codex Autopilot.` to complete any interrupted plan transaction,
  reconcile running ownership and locks, then create fresh work when safe.
- Send `What is Codex Autopilot doing right now?` for deterministic local status without a model request.

The whole message must be the phrase - a control word inside a sentence is an
ordinary request. Since 0.10 the bare words work on their own: `stop`, `pause`,
`останови`, `пауза` pause the run; `resume`, `continue`, `продолжи`, `возобнови`
resume it; `status`, `статус`, `tasks`, `задачи`, `show tasks`, `покажи задачи`
answer with the status card. Trailing punctuation and a leading `just`, `please`,
`просто`, `давай`, `давайте`, or `пожалуйста` are ignored. Uninstall is deliberately
not in this list: it requires the product name.

The status report groups milestones under `Running`, `Verifying`, `Waiting`,
and `Ready`; explains each wait; shows verified progress plus worker and
Computer Use capacity; and lists exact active task titles. It also reports the
canonical target cwd and distinguishes verified App Server project metadata
from Desktop placement that remains Codex App-authoritative.

After a crash or reboot, embedded launch descriptors, reservation tokens, resource locks, and the lifecycle journal remain authoritative. Unknown create or production-send outcomes stay `AMBIGUOUS` and are not retried automatically. A known App Server thread is never replaced merely because later metadata or handoff proof is incomplete. Dependencies unlock only after an authoritative Desktop Stop, checkpoint and memory evidence, and a fresh independent verifier PASS against the original user request, task specification, and every DoD item. See [Desktop-owned runtime](docs/DESKTOP_RUNTIME.md).

Workers can also request a typed prerequisite, dependency, resource, or
verification change. Autopilot drains active work, runs one fresh bounded
replanner, revalidates the complete graph, and commits the next graph version
through a recoverable transaction. See
[Plan evolution and recovery](docs/PLAN_EVOLUTION_AND_RECOVERY.md).

## Advanced local CLI

The executable is:

```text
~/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot
```

It is intentionally not added to PATH. Run it with `--help`: it lists exactly the
commands meant to be typed by hand - `bootstrap`, `preflight`, `timeline`,
`unblock`, `authorize-project-root`, `status`, `stop`, `resume`, `logs`, `doctor`,
`skills`, `revoke-skill`, and `uninstall`. `resume` is there only as a pointer: it refuses and tells you to
send the resume phrase in a Codex task, because launching belongs to the trusted
Stop hook. The Pipeline Engineer recovery set - `relay-status`, `relay-complete`,
`relay-fail`, `reconcile-thread-identity`, `recreate-archived-retry`, `arm`,
`devops-rearm-relay-owner`, `devops-repair-runtime`, `devops-revert-runtime-patch`,
and `devops-resolve-incident` - and the internal entry points are kept out of
`--help`: the on-call engineer runs them, not you.

## Watching a run

Say `status` in any task of the project. The hook answers directly, without a
model turn, in a few lines: progress, what is running, what blocks it. Say
`status detail` for the full report. Codex labels a hook answer as a blocked
message; that means the hook replied instead of the model, not that anything
failed.

The sidebar is a separate matter. Desktop runs its own App Server process,
Autopilot drives another, and the two share only the filesystem, so nothing can
tell the app that a task appeared. A created task becomes listable about a second
after its turn starts, but the sidebar shows it when the app next re-reads its
list - which your own activity triggers. To learn that work started or finished
without watching the sidebar, turn on the system banner:

```toml
# .codex-autopilot/config.toml
[runtime]
desktop_notifications = true
```

It is off by default, because a banner is a side effect on your machine and
those are not switched on silently. Three events raise one: a task taken into
work, a task verified or stopped, and the run finished.

Every worker opens its turn with a short brief - what it took, what it will
deliver, by which route, who judges it, and which files it holds for writing -
before it reads a single file. Opening the task tells you where it is without
reading the whole transcript.

## Disk

The run writes the whole App Server conversation to
`.codex-autopilot/logs/`, one file per dispatcher. These are debugging traces,
not the run's memory, and they are by far the largest thing in the project: on
one real run, 2.3 GB across 167 files while the journal, plan and project
memory together were a few megabytes.

The runtime keeps them bounded itself. Each dispatcher sweeps finished traces -
whole files, oldest first - until the directory fits:

```toml
# .codex-autopilot/config.toml
[runtime]
log_retention_mb = 512   # 0 keeps everything
```

The newest five traces survive whatever the budget says, and nothing written in
the last hour is removed, so a live dispatcher never loses its own file. Deleting
old traces by hand between runs is also safe.

The other space is `staged-artifacts/`: a task with a filesystem deliverable runs
against a full copy of the project tree rather than the project itself, one copy
per such task, and those are not swept. Removing a finished run's copies is your
own `rm -rf`.

## Uninstall

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" uninstall --yes
```

This removes the Autopilot plugin and marketplace registrations, the wake-up launch agent, this version's runtime directory, a `current` symlink that points to it, and the temporary launch registry. It preserves shared Python/Codex/Homebrew/Git installations, the `legacy-backups` archives, any installation set aside as `<version>.repaired-<stamp>`, source repositories, and project state.

To set one project's Autopilot state aside too (it is moved to a sibling `.codex-autopilot.purged-<stamp>`, never deleted; the path is printed, and you remove the sibling yourself when you are sure):

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" uninstall --yes --purge-project-state --project /absolute/path/to/project
```

## A task stopped by a rule

Autopilot does not lift such a stop by itself: a rule violation is reviewed
by a human, and «resume» deliberately does not erase it. When you have looked
into it and decided the work may go on, lift the stop by your own decision with
`codex-autopilot unblock` — the reason is recorded in the run state:

```bash
"$HOME/Library/Application Support/CodexAutopilot/current/bin/codex-autopilot" unblock --project /absolute/path/to/project --task <ID> --reason "<why this is acceptable>"
```

After that, continue the run with the usual phrase «Resume Codex Autopilot.»
in a Codex task.
