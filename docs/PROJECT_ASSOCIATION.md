# Canonical project association

Every run stores one resolved `target_project_root`. Every thread is filed
with that directory as `cwd` - it is what Desktop files the thread by - and a
task whose work is staged writes only its staged workspace, given as
`runtimeWorkspaceRoots` (placement contract 2, `placement_contract.py`). Both
are sent again on every `turn/start`, since the server rewrites a thread's cwd
on each turn. The initiating task's directory is not a substitute.

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
to the on-call: a runtime that filed a thread with the wrong cwd is repaired; a
missing Desktop root or the isolation trade-off goes to her with a diagnosis,
a recommendation and the run's threads outside the project by id and title.
Threads created before this check - recorded INSIDE by `projectId` alone - are
listed once in their own ticket; Autopilot does not move a saved thread (App
Server has no measured call for it, and Desktop's state file is Desktop's), she
can move them in Desktop.

## Isolation before placement

Contract 2 is used for a staged task only after `isolation_probe.py` proved
that a thread filed at the root with the workspace as its only runtime root
cannot write the root: `command/exec` in an ephemeral thread on the project
root itself, the disk as ground truth, a permission request never answered
(it counts as "not proven"). Preflight measures it: a writable root fails with
an ISOLATION finding; "not proven" keeps the old placement for staged tasks
and says so. The result is kept in `.codex-autopilot/isolation-probe.json`
with the Codex binary identity and the Desktop version; a run without a
record for its root, profile and binary - a run paused before this change - is
measured by the dispatcher before its first staged thread. Sessions created
before contract 2 keep their old cwd checks to the end of their life.

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
