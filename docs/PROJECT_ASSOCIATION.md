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
defect on its session and in the journal, and one ticket per cause per run
(`placement_defects.py`, stop kind `placement_defect`) that holds no task goes
to the on-call. The cause is part of the ticket's signal id, so two causes of
one run are two tickets, each with its own diagnosis, even while the other is
open. A runtime that filed a thread with the wrong cwd, or whose staged
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
truth; a permission request is never answered and counts as "not proven".

No outcome stops the run or asks her. PASS enables contract 2. ROOT_WRITABLE
or NOT PROVEN is reported by preflight as an ISOLATION finding, staged tasks
keep their workspace as cwd (isolated, outside the project), and each such
thread's R5 ticket takes the record to the on-call as a runtime defect. The
record is kept in `.codex-autopilot/isolation-probe.json` with the Codex binary
identity and the Desktop version; a record of another shape (another root,
profile, binary, record version, or a workspace outside the state directory)
does not count, and the dispatcher measures again - on a short-lived server of
its own, before it launches the task's server with the profile - which is how
a run paused before this change is measured when it resumes. Sessions created
before contract 2 keep their old cwd and profile checks to the end of their
life.

## After a thread is visible

A visible thread can be opened by her, and Desktop then rebuilds its runtime
roots from its cwd and its own writable roots; a turn Desktop runs is served
by Desktop's App Server, which does not define the staged profile. Nothing
Autopilot sends can prevent that turn, so it is watched (`isolation_guard.py`):

- before each of its own turns Autopilot reads the thread's
  `environments[].runtimeWorkspaceRoots` (and the resume answer's roots);
  roots wider than the workspace are recorded on the session and signalled
  once per run, and the turn that follows replaces them with the workspace
  under the staged profile;
- after the task's promotion the canonical root is compared with the task's
  manifest: a path changed since staging that neither this promotion nor a
  later promotion of another task wrote goes to the on-call with the paths and
  whether roots were seen widened. Nothing is held.

Preflight requires the run's root to be one of the Desktop project's roots
exactly, as Desktop compares them; a root below a project root fails, and a
missing Desktop state file is reported as UNOBSERVABLE, not as a pass.

When App Server exposes a saved project containing the target, preflight chooses
the unique longest matching root (or a validated explicit target project), sends
that project ID with the canonical cwd, and attests returned cwd/project/title
metadata. Desktop sidebar project IDs and App Server project IDs are separate
namespaces and must not be compared or substituted.

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
