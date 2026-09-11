# Parallel execution

The v0.9 candidate has a deterministic, non-LLM scheduler. For a schema-3 plan
configured as `parallel` or `auto`, it can reserve multiple independent READY
tasks up to the lower of the plan and durable-state `max_parallel_workers`
limits. It admits only tasks whose dependencies, capabilities, Computer Use
capacity, and resource snapshot allow them to run.

The priority order is stable: resource availability, explicit priority,
remaining critical-path length, transitive fan-out, logical READY age,
declaration order, then task ID. Model identity and reasoning effort do not take
part in scheduling.

The selected filesystem strategy is one shared working tree protected by
declared resource claims. `git.auto_commit=false` remains the default. The
runtime does not attempt to merge application-owned or binary output; matching
write/exclusive claims serialize such work.

## Current release blockers

- New schema-3 plans and the documented bootstrap example currently default to
  `serial` with one worker, although the v0.9 product contract says `auto` is the
  default experience.
- Every production worker must edit the same advisory `HANDOFF.md`, but that
  shared write is neither represented by a resource claim nor validated by a
  task-specific checkpoint. Parallel reservations receive the same pre-run file
  hash, so one worker's edit can satisfy another worker's checkpoint gate and
  concurrent edits can overwrite each other.
- The repository contains deterministic fake coverage, but no v0.9 live
  multi-worker run was executed during the independent M10 audit.

See `SCHEDULER.md`, `RESOURCES.md`, and
`RELEASE_VERIFICATION_0.9.0-beta.md`.
