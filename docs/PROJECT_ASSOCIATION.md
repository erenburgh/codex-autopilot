# Canonical project association

Every run stores one resolved `target_project_root`. All implementation,
verification, revision, planner, and replanner payloads must use that directory
as `cwd` and as the runtime workspace root. The initiating task's directory is
not a substitute.

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
