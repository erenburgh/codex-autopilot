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

## Defaults and what is not proven here

- A new schema-3 plan defaults to `execution_strategy="auto"` with up to ten
  workers and one Computer Use slot (`plan.py`), and both skill templates
  declare the same. A migrated v0.8 plan keeps `serial` with one worker and
  never enters parallelism implicitly.
- The completion checkpoint is per task: each worker writes
  `.codex-autopilot/handoff/<task-id>.md` and the gate checks that one file
  (`lifecycle_base.py`). The shared `HANDOFF.md` is a note for the human, not
  a gate, so parallel workers no longer share one hash or overwrite one file.
- The repository suite drives fake App Server and Desktop clients. It proves
  reservation, resource and dependency behaviour; it is not evidence of a live
  multi-worker run.

See `SCHEDULER.md` and `RESOURCES.md`.
