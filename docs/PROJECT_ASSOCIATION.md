# Canonical project association

Every run stores one resolved `target_project_root`. Every thread is filed
with that directory as `cwd` - it is what Desktop files the thread by - and a
task whose work is staged writes only its staged workspace: it is given as
`runtimeWorkspaceRoots`, under the task's own staged permission profile
(placement contract 2, `placement_contract.py`). Cwd, roots and profile are
sent again on every `turn/start`, since the server rewrites a thread's cwd on
each turn. The initiating task's directory is not a substitute.

## How Desktop files a thread (R5)

Desktop does not file a thread by App Server's `projectId`. Its own rule, read
out of the Desktop bundle (26.917.62051) and repeated in `desktop_sidebar.py`:

1. a record in `thread-project-assignments` puts the thread in that project
   (an assignment of a ChatGPT project, or one marked projectless, puts it in
   none; an assignment to a project Desktop has no group for decides nothing);
2. a thread in `projectless-thread-ids` is in no project;
3. otherwise the thread's cwd must EQUAL one of a local project's
   `rootPaths` (or its `rootPathAliases`/`pathAlias`), compared the way
   Desktop normalizes paths: backslashes to `/`, lower case, a trailing `/`
   kept. A subfolder of a root is in no project.

So a staged worker used to be invisible in the project: its cwd was
`<root>/.codex-autopilot/staged-artifacts/<task>/workspace`, while the old
check called it INSIDE because its `projectId` matched. The placement check
(`launch_gate.measure_placement`) now records two facts apart - App Server
`projectId` ok, and Desktop's rule with its reason and the Desktop version it
was measured on - and reports INSIDE only when both hold. Missing Desktop state
or a missing project is UNOBSERVABLE, never INSIDE.

Placement never stops work. A thread measured outside the project is an R5
defect on its session and in the journal, and a ticket that holds no task
(`placement_defects.py`, stop kind `placement_defect`) goes to the on-call -
one per cause while it is open. The cause is part of the ticket's signal id,
so two causes of one run are two tickets, each with its own diagnosis, even
while the other is open. A cause that comes back after the on-call closed its
ticket files a new one, so a repair that did not hold is seen; the door's R23
bound sends the third such ticket to her with the report. A runtime that filed a thread with the wrong cwd, or whose staged
profile did not hold, is repaired by the on-call; only a missing Desktop root
(R6) goes to her, with a diagnosis, a recommendation and the run's threads
outside the project by id and title.
Threads created before this check - recorded INSIDE by `projectId` alone - are
listed once in their own ticket; Autopilot does not move a saved thread (App
Server has no measured call for it, and Desktop's state file is Desktop's), she
can move them in Desktop.

## Isolation before placement

A thread filed at the root must still not write the root before an
independent PASS. What protects it is measured, not assumed
(`isolation_probe.py`; `codex sandbox` of codex 0.154.0 on a scratch tree):

- the built-in `:workspace` profile writes its `:workspace_roots`, which
  default to the cwd - so with `runtimeWorkspaceRoots = [workspace]` the root
  would be read-only only if every turn materialized its roots from them and
  nothing widened them;
- so each staged task runs under its own profile, `codex-autopilot-staged-<hash>`:
  `extends` the run's profile, with `filesystem = {":workspace_roots" = "read",
  "<workspace>" = "write"}`. It keeps the root read-only and the workspace
  writable whether `:workspace_roots` is the workspace or the root. It is
  defined by `-c` overrides for the one App Server process that serves the
  task - no config file of hers is written - and every `thread/start` and
  `turn/start` of the task names it.

The probe measures exactly that profile: `command/exec` on the project root
itself (`cwd = root`, the worst case), with that `permissionProfile` - never
a legacy sandbox policy - and a workspace under `.codex-autopilot/`, as every
staged workspace is. An ephemeral thread started as a worker's must answer
with no wider roots and with that profile active. The disk is the ground
truth; a permission request is never answered and counts as "not proven" -
a request on the root write alone, with the workspace write done, is NOT
PROVEN, never PASS.

No outcome stops the run or asks her. PASS enables contract 2. ROOT_WRITABLE
or NOT PROVEN is reported by preflight as an ISOLATION finding, staged tasks
keep their workspace as cwd (isolated, outside the project), and each such
thread's R5 ticket takes the record to the on-call as a runtime defect. The
record is kept in `.codex-autopilot/isolation-probe.json` with the Codex binary
identity, the runtime code that measured it (a digest of the runtime's modules)
and the Desktop version; a record of another shape (another root, profile,
binary, runtime code, record version, or a workspace outside the state
directory) does not count, and the dispatcher measures again - on a
short-lived server of its own, before it launches the task's server with the
profile - which is how a run paused before this change is measured when it
resumes.

A failed measurement does not stay for the run. NOT PROVEN (a probe timeout, a
server that did not start, a permission request) stands for ten minutes and is
then measured again before the next staged thread. ROOT_WRITABLE is a
measurement on one binary and one runtime code and stands until either
changes: a runtime repair (devops-repair-runtime), once installed, changes the
runtime code, and the next staged thread is measured on the repaired code.
Before, the record counted by its shape alone, so one transient failure kept
every staged task on contract 1 to the end of the run.

## Sessions from before contract 2

They carry no `placement_contract`, and their thread's cwd is their staged
workspace. They keep their old cwd and profile to the end of their life:

- a PREPARED one is resumed by the dispatcher on its own thread: the cwd it
  must have is its workspace (`session_cwd`), its turn names the run's profile
  (`session_profile`) with the workspace as its runtime root, its App Server is
  launched without a staged profile (`server_overrides` gives a created thread
  one only if it was created under it), and the widened-roots check - a
  contract 2 check - does not apply;
- an ACTIVE one - the paused beyondness run's M01 verifier `01a0cf05`, whose
  dispatcher is gone - is never resumed. When she resumes the run, resume asks
  the server about every pending session; a finished thread (`notLoaded`)
  retires the attempt to RETRY_WAIT and releases its locks. The task's next
  attempt is a new thread, filed at the root under the staged profile when the
  isolation record stands PASS (measured first if there is none). The old
  thread stays where Desktop put it, outside the project, and is named by id
  and title in the run's `created_before_the_honest_check` ticket. A thread
  the server still reports running is kept, as before: "I do not know" is not
  "it ended";
- the on-call's relay repair accepts exactly the shapes `thread_placement`
  makes (`placement_contract.repair_contract_ok`): the task's workspace as
  its only runtime root, with the root and the staged profile (contract 2) or
  the workspace and the run's profile (contract 1); anything else - the old
  `[root]` roots, wider roots, a cwd outside the root, the root with the run's
  profile - is refused.

## After a thread is visible

A visible thread can be opened by her, and Desktop then rebuilds its runtime
roots from its cwd and its own writable roots; a turn Desktop runs is served
by Desktop's App Server, which does not define the staged profile. Nothing
Autopilot sends can prevent that turn, so it is watched (`isolation_guard.py`):

- before each of its own turns Autopilot reads the thread's
  `environments[].runtimeWorkspaceRoots` (and the resume answer's roots);
  roots wider than the workspace are recorded on the session and signalled
  (one ticket while it is open, a new one if they widen again after it was
  closed), and the turn that follows replaces them with the workspace
  under the staged profile;
- after the task's promotion the canonical root is compared with the task's
  manifest: a path changed since staging that neither this promotion nor a
  later promotion of another task wrote goes to the on-call with the paths and
  whether roots were seen widened. Nothing is held.

The run's threads Desktop files outside the project are listed in the R5
tickets by id and title (`outside_threads`). A thread whose placement cannot be
read - no Desktop state file, no project - is listed apart
(`unobserved_threads`) and is never claimed outside: with the state unreadable
every thread measures UNOBSERVABLE, and the first version put all of them, the
ones filed at the root included, under "Desktop files them in no project".

Preflight requires the run's root to be one of the Desktop project's roots
exactly, as Desktop compares them; a root below a project root fails, and a
missing Desktop state file is reported as UNOBSERVABLE, not as a pass.

When App Server exposes a saved project containing the target, preflight chooses
the longest matching root (or a validated explicit target project), sends
that project ID with the canonical cwd, and attests returned cwd/project/title
metadata. Desktop sidebar project IDs and App Server project IDs are separate
namespaces and are never substituted for each other; they are paired only
through Desktop's own map, `app-server-project-id-by-legacy-project-id-by-host`
in `.codex-global-state.json` - a dictionary, so one Desktop project links
exactly one App Server project.

Two App Server projects on the run's root used to stop every start ("multiple
saved Codex Projects match this path"): on the beyondness run the initiating
agent had created one of its own with `project/create`. Now the project Desktop
links wins the tie; without a link, the one Desktop shows, then the lowest id
(App Server ids are UUIDv7, so the oldest). An explicit
`--app-server-project-id` that differs from the linked holder is overruled the
same way. Each case is a WARN finding, not a stop. The one refusal left is
`ID_PAIR_MISMATCH` FAIL: Desktop links its project to an App Server project
that does not hold the run's root, so no choice is a consistent pair; the
finding says what to do in Desktop.

## The roots audit (R6)

`project_roots_audit.py` compares the run's root with Desktop's
`local-projects[<desktop id>].rootPaths`, the App Server projects and the id
map. Read-only; findings, each WARN unless noted:

- `SIBLING_ROOTS` - another root looks like a copy of the run's root: at
  least two of the same folder name, the same top-level markers (`*.uproject`,
  `AGENTS.md`, `.git`) and the same repository name in a remote. Two roots
  alone are not a finding. Only top-level names are listed; a file the iCloud
  File Provider evicted (`st_flags & SF_DATALESS`) is never opened; the remote
  is parsed out of `.git/config`; git runs only for a `.git` pointer file and
  always with a timeout;
- `ACTIVE_ROOT_MISMATCH` - the project's first root is not the run's root, so
  new chats in the project open elsewhere. Desktop's `active-workspace-roots`
  is global UI state and counts only while `selected-project` is this project;
- `DUPLICATE_APP_SERVER_PROJECTS` - another App Server project holds the root,
  marked visible in Desktop or App Server only;
- `ID_PAIR_MISMATCH` - see above;
- `ROOTS_DIVERGED` - Desktop's rootPaths and the linked App Server project's
  roots differ;
- `UNVERIFIED` - a key of Desktop's undocumented format is missing: "could
  not check", never a pass and never a stop.

It runs at preflight (printed as `Project roots <CODE>` lines), before every
thread creation, and at every wake-up (Desktop's side only; a run paused
before the audit existed is audited at its first wake-up at the user's Codex
home). The latest audit is `roots_audit` in the run state, and a change of
findings is a `roots_audit` journal event. Each finding that asks for her
decision is a proposed decision in Project Memory (`origin=environment`,
scope `codex-project-roots`) whose text carries the recommendation and the
ready command; one per finding, however often the audit runs, and a rejected
one is not proposed again. When a later audit of the same kind no longer sees
the finding, the runtime supersedes the proposal with the evidence. The status
card carries one line - "Codex project: 2 roots, the active one is a copy ...;
see decision D-..." - while a proposal is open.

## Changing a saved project

Desktop keeps `local-projects` as its source, sends `project/update` itself
when she edits a project, and never reads App Server's roots back. So roots
are fixed where she sees them: Codex Desktop -> project -> Edit project. That
edit writes both spaces, needs no record, and the audit closes the proposal
afterwards.

`authorize-project-root` carries her decisions, each confirmed by typing the
project id at an interactive terminal and refused inside a Codex task
(`CODEX_THREAD_ID`), never by `--yes`:

- `--retire-duplicate <id>` - `project/delete` of an App Server project that
  Desktop does not show, holds the run's root, is not the run's project and
  has no threads (read from App Server's state database, read-only). The
  decision `AUTOPILOT_PROJECT_DELETE project_id=<id> root=<path>` is recorded
  first, a restorable snapshot is written, the project list is read again;
- `--remove-root <path>` / `--set-primary-root <path>` - App Server follows
  Desktop only: refused, with the Desktop instruction, unless Desktop already
  lists the wanted roots. `AppServerClient.replace_project_roots` sends
  nothing without her exact decision (`AUTOPILOT_PROJECT_ROOT_REMOVE` /
  `_PRIMARY`, project and path) and refuses a list without the run's root;
  the audit runs again and a remaining divergence is said, not called done;
- `--decline <decision id>` - she rejects a proposal; it is not raised again.

A command given without `--project` from a root that has no run, while
another root of the same Desktop project has one, uses that run and says so;
`bootstrap`, `start-skill` and `preflight` refuse there and name the run's
path, since they would start a second run in the copy.

If no saved project contains the target, the supported fallback is an
unassigned thread with the canonical cwd plus an explicit limitation report.
The initiating task may be outside the target and must not cause target workers
to be placed into its unrelated saved project.

## Candidate status

`resolve_preflight_project()` currently falls back to a saved project matching
the initiating cwd. Preflight then creates the target-cwd task unassigned and
moves it into that unrelated project. The independent contract regression
demonstrates the mismatch. This must be corrected before project-association
acceptance can pass. No live Desktop project/sidebar association was exercised
in M10.
