# Install and uninstall footprint

## Default installation

`install.sh` creates or replaces the directory of the version it installs (`0.13.1-beta` in this release), and writes two things outside it: one launch agent, and one marked block in the Codex execpolicy.

```text
~/Library/Application Support/CodexAutopilot/
├── <version>/
│   ├── venv/                   # Python venv without pip
│   ├── runtime/                # the repository tree: src/, tests/, docs/,
│   │                           # scripts/, plugins/, install.sh, pyproject.toml
│   ├── bin/codex-autopilot
│   ├── plugins/
│   ├── .agents/
│   └── user documentation
├── <version>.repaired-<stamp>/ # only when the replaced install carried accepted repairs
├── legacy-backups/
│   └── previous-installs-<stamp>.zip   # earlier installations, zipped and then removed
├── projects.json               # projects the wake-up sweep visits; written when a run is armed
├── wake-sweep.log              # output of the wake-up agent; emptied when it passes 1 MB
└── current -> <version>

~/Library/LaunchAgents/
└── com.codex-autopilot.wake.plist   # wake-up agent: RunAtLoad plus StartInterval 300

$CODEX_HOME/rules/            # normally ~/.codex/rules/
└── default.rules             # one marked block: the installed script may be
                              # launched with start-skill and timeline
```

That second file belongs to Codex, not to Autopilot. The block is delimited by
the marker `# codex-autopilot (managed)` and contains exactly two
`decision="allow"` rules, for `start-skill` and `timeline` on the installed
script path and nothing else - `devops-*`, `uninstall`, `hook` and `relay-*`
still ask. It exists because Codex raises a native approval dialog for a
command that matches no rule, the dispatcher answers no approval by rule, and
the dialog would wait in a task nobody is looking at while the run stands
still. Rules written by you or by Codex are read and rewritten back
unchanged; only the marked block is replaced on reinstall and removed on
uninstall.

`runtime/` is the repository tree, not `src` alone: `tests/` is copied next to it because the on-call engineer proves a repair by running that suite against the installed copy.

It registers a local marketplace named `codex-autopilot-local` through the official CLI and installs exactly one of `codex-autopilot-adaptive` or `codex-autopilot-host-settings`. Codex owns the resulting registry/cache data under its normal home. The installed profile's `.mcp.json` points to the absolute `current/bin/codex-autopilot` launcher, not to the version directory.

When old local preview skills named `astra-autopilot-adaptive` or `astra-autopilot-inherit` exist under `~/.codex/skills`, the installer moves them into `~/Library/Application Support/CodexAutopilot/legacy-backups/` instead of deleting them.

With `--install-deps`, missing Python may be installed with `brew install python@3.13` when Homebrew already exists. A missing Codex CLI may install Node through Homebrew and `@openai/codex` through npm. Homebrew itself is never installed. These shared dependencies are not removed by Autopilot uninstall.

## The wake-up agent

The installer writes `~/Library/LaunchAgents/com.codex-autopilot.wake.plist` and loads it with `launchctl bootstrap gui/<uid>`. The agent runs `codex-autopilot _wake-sweep` once at login and then every 300 seconds, for as long as it stays installed, and appends its output to `~/Library/Application Support/CodexAutopilot/wake-sweep.log`.

One sweep reads the project list from `projects.json` and decides per project. A project whose `.codex-autopilot/config.toml` is gone, whose run is paused or `DONE`, or which has nothing due, is skipped. `BLOCKED` is no longer a reason to skip: it is derived, and means only that everything left waits for the owner. Something is due when a task waits in `RETRY_WAIT` with a due retry time, or when a started run is stranded with no live dispatcher: a ticket waits for the on-call with no engineer session, work could be taken and no session is pending, or a pending session lost its dispatcher - whatever its status, since the dispatcher is what consumes a turn. A create in doubt whose task an open ticket already holds is that ticket's, not the wake-up's. Where something is due and no live wake-up process is already waiting for it, the sweep arms one. When nothing can be raised on the run's behalf - no completed causal owner, or revoked hook trust - the owner is told once per cause instead of the sweep passing by in silence. That process sleeps until the time comes, passes the same hook-trust and ownership gate as a hook-driven launch, and then raises the dispatcher. A session whose dispatcher died after its thread existed is settled by the server's word, as her Resume settles it: the wake-up reads the thread (`thread/read`, no model request), retires a turn that is over - a worker goes to `RETRY_WAIT`, an engineer frees the on-call's lane - and reserves what that freed; a turn still running, or one the server cannot answer for, is left alone and asked about again on the next sweep. The sweep itself starts no worker, makes no model request, and wakes no run a human stopped. It exists because the sleeping wake-up process does not survive a reboot.

The sweep prints what it did, not what it looked at. It names a wake-up it armed and a project it could not read, and reduces everything else to one counted line. Until 0.12.3 it printed a line per registered project every five minutes; `projects.json` keeps every project ever created, including the temporary ones test runs leave behind, so on the author's machine the log had reached 55 MB and 596,661 lines, almost all of them reporting that a temporary directory was still gone. The agent is now also told where its own log is, and empties it when it passes 1 MB - launchd rotates nothing, and the file is a heartbeat, not evidence. It is emptied rather than deleted, because launchd opens it before the sweep starts.

`CODEX_AUTOPILOT_SKIP_LAUNCHD=1` at install time still writes the plist but does not load it into launchd. `codex-autopilot uninstall --yes` boots the agent out and deletes the plist.

## Previous installations

Earlier installations are not left standing beside the new one. Once the new version is in place, every other versioned directory in the install root is added to `legacy-backups/previous-installs-<stamp>.zip` and then removed; the installer prints how many were packed and where the archive is. `current`, `legacy-backups` and the version being installed are left alone.

One directory escapes this. An installation that carries accepted runtime repairs - a non-empty `runtime/patches` - is renamed to `<version>.repaired-<stamp>` beside itself before the new install replaces it, the installer prints that path, and the archive loop skips it. Nothing in it is deleted or zipped.

## Project footprint

After successful preflight and initialization, the target Git repository receives:

```text
ROADMAP.md
.codex-autopilot/
├── config.toml
├── plan.json
├── run-state.json
├── MILESTONE.md
├── HANDOFF.md
├── PROJECT_STATE.md
├── DECISIONS.md
├── memory.sqlite3             # WAL/SHM may exist while open
├── memory-backups/latest.sqlite3
├── handoff/                  # one checkpoint per task; the completion gate reads it
├── launches/
├── pipeline-incidents.json   # the on-call engineer's tickets
├── rate-limits.json
├── bootstrap-plan.json
├── logs/
├── skills/                   # installed Skill Pack manifests, if any
├── hired-skills/             # bundles a screening hired, if any
├── staged-artifacts/         # one copy of the project per isolated task - see below
└── migrations/               # only when migrating old state
```

`staged-artifacts/` is the second-largest thing Autopilot puts in a project -
`logs/` below is the first, by a wide margin - and it is worth knowing about
before a long run. A task that declares a
filesystem deliverable and can write through a filesystem resource
(`task_requires_staging`) does not touch the project directly: the runtime
copies the whole project tree into
`staged-artifacts/<task-id>/workspace` and runs the worker there, then promotes
the result. The copy skips `.git`, the state directory, and the usual caches
(`.venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`) - and
nothing else, so it is roughly the size of your working tree, once per such
task.

Nothing removes those copies. There is no `rmtree` in `artifact_staging.py`:
a promoted workspace, an abandoned one and the `promotion-snapshots/` beside
them all stay until you delete them. On a graph with several isolated tasks the
project directory grows by several times the size of the repository, and
`--purge-project-state` moves that weight aside rather than freeing it. Deleting
a finished run's `staged-artifacts/` is safe and is your own `rm -rf`.

`logs/` is the one that actually fills a disk, and nothing in the runtime
rotates, truncates or removes it. Every dispatcher writes the whole App Server
wire conversation, both directions, to its own
`logs/app-server-dispatcher-<token>.jsonl`, plus one `automatic-relay-<token>.log`
per relay. Measured on the author's own run: 167 files, 2.3 GB, individual
traces between 70 and 108 MB - against 28 MB of staged workspaces and 6.3 MB of
run journal in the same project. The whole project directory was 2.4 GB, and
2.3 of them were these traces.

They are debugging traces, not the run's memory. The state the runtime needs is
`run-state.json`, `plan.json`, `memory.sqlite3`, `memory-backups/` and
`handoff/`, and all of those together are a few megabytes.

Since 0.11.6 the runtime keeps them bounded itself. Every dispatcher sweeps the
directory before it opens its own trace: whole files, oldest first, until
`runtime.log_retention_mb` is met (512 MB by default). The newest five are kept
whatever the budget says, and nothing written in the last hour is touched - the
dispatcher writing right now owns one of those. Nothing is ever truncated: a
capped trace loses its tail, and the tail is where the failure is. Only this
runtime's own `app-server-*` and `automatic-relay-*` files are eligible;
anything else in that directory is left alone.

```toml
# .codex-autopilot/config.toml
[runtime]
log_retention_mb = 512   # 0 keeps everything
```

Deleting old `app-server-dispatcher-*.jsonl` by hand is still safe; keep the
newest few if an incident is open, because that is where the on-call engineer
reads what the server actually said.

`skills/` is read, never written, by Autopilot: one JSON manifest per exact Skill Pack revision, put there by you. It is the second half of the skill catalog, beside the packs a plan declares. Initialization does not create it.

`hired-skills/` is different and is written by Autopilot. When screening is on (`runtime.skill_screening`, on by default), a task may be given a skill bundle — a `SKILL.md` and the files beside it — fetched from a public repository. The bundle is copied into `hired-skills/<name>@<digest>/` inside the project and nowhere else: `hired_skills.admit_skill_bundle` resolves every destination under that one directory and refuses any other path, so the Codex plugin cache, `~/.codex/skills`, hooks and MCP configuration are not reachable from it, and a bundle that ships `.codex-plugin`, `hooks`, `.mcp.json` or `commands` is refused outright. `codex-autopilot skills --project <path>` lists what a project holds; `codex-autopilot revoke-skill --project <path> --skill-id <id>` removes one.

Nothing is installed into your Codex. Autopilot reads `$CODEX_HOME/skills` to see the skills you installed yourself, so a task can be given one you already use, and it uses that skill where it is rather than copying it; it never writes there. Uninstalling Autopilot does not touch your own skills, and `--purge-project-state` sets a project's hired bundles aside with the rest of its state - moved to `.codex-autopilot.purged-<stamp>`, never deleted.

Preflight creates no project run-state when it fails. Its disposable SQLite/FTS5 probe is removed.

## What is unchanged

Installation does not add PATH entries, edit shell profiles, edit Git config, initialize or commit a repository, alter account defaults, change Codex model/reasoning/speed/sandbox/network settings, grant macOS permissions, or modify unrelated plugins. The two things it adds to the system outside its own directory are the wake-up launch agent and the marked execpolicy block described above, and uninstall removes both. Adaptive sends only per-worker model and effort fields. Host Settings sends neither.

## Uninstall

`codex-autopilot uninstall --yes` pauses an identifiable dispatcher of its own version, removes the two plugin registrations and the marketplace registration, boots out and deletes the wake-up launch agent, removes the marked block from `$CODEX_HOME/rules/default.rules` (deleting that file only when it held nothing else), removes its own version directory, removes `current` only when it points there, and clears the temporary launch registry (that directory is still named `codex-autopilot-<uid>-v0.8`, and the name is the only thing about it that is still v0.8). It preserves the rest of the install root - `legacy-backups`, any `<version>.repaired-<stamp>` tree, `projects.json`, `wake-sweep.log`, and any older installation still present - along with shared tools, source repositories, and every project's state.

Project state is set aside only with `--purge-project-state --project <absolute-path>`. This moves that project's `.codex-autopilot` directory to a sibling `.codex-autopilot.purged-<stamp>` (the path is printed) and never deletes it: removing state without a restorable snapshot is refused by rule R28, so freeing the space is your explicit `rm -rf` of that sibling. It does not touch `ROADMAP.md`, project source, Git metadata, or commits. Likewise `start-skill --replace` copies the previous state to `.codex-autopilot.replaced-<stamp>` before overwriting it, and records that path in the new run's journal.
