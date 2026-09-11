# Dynamic specialist roles

A schema-3 plan declares arbitrary `RoleProfile` objects. Each role contains a
stable ID, human-readable name, responsibilities, and optional domain focus,
preferred tools, context priorities, and verification expectations. Tasks and
independent verification policies reference those IDs; the runtime never
infers a missing production role from task prose.

Roles shape the bounded fresh-worker prompt, not model routing. Execution mode
selects Sol for code/general work and Astra for required Computer Use. Verifier
execution mode may differ from the implementer's. Reasoning is routed
separately.

`AIStudioRuntime` is stateless and rebuilds every implementation, verification,
revision, planning, and replanning prompt from the task graph and selective
Project Memory. A role is reusable metadata; it is not a durable worker session
or a persistent manager.

The initiating planning turn is responsible for choosing useful roles from the
goal. The normal UX must not require the user to name the team manually.

See `AI_STUDIO_RUNTIME.md`, `DEPENDENCY_GRAPH.md`, and `MODEL_ROUTING.md`.
