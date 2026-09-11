# Resource claims and locks

Tasks may claim `path`, `directory`, `glob`, `application`, `environment`,
`browser`, `device`, `external_sandbox`, or `logical` resources with `read`,
`write`, or `exclusive` access. Filesystem claims are normalized under the
canonical project root without expanding environment variables. Named claims
use normalized, case-insensitive identities.

For overlapping targets, read/read is allowed; read/write, write/write, and any
match involving exclusive access conflict. Directory and glob overlap is
checked deterministically. A Computer Use task also consumes a separate shared
slot; the conservative default is one.

Acquisition, task transition, reservation identity, and durable journal update
share one coordinator transaction. Locks identify run, task, attempt, worker,
ownership token, and—once known—thread and turn. Recovery releases a lock only
after an authoritative terminal/absent result. Unknown ownership remains locked
and opens a structured infrastructure incident instead of permitting a second
writer.

The runtime uses one shared working tree and does not imply automatic merging.
Planner accuracy still matters: undeclared project writes cannot be inferred by
the lock layer. The M10 audit additionally found that the required shared
`HANDOFF.md` write is not represented safely for parallel workers; see
`RELEASE_VERIFICATION_0.9.0-beta.md`.

See `RESOURCES.md` for normalization and recovery details.
