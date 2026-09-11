# Resource coordination and the shared working tree

The v0.9 resource layer is deterministic local code. It converts each task's
declared resource bundle into normalized identities, checks the whole bundle,
and records one durable ownership lease before a worker may start. Acquisition
is all-or-nothing under the project resource-coordinator file lock; a denied
request changes no state.

The coordinator supplies `SchedulerAvailability.resource_available` for held
locks and `resource_conflicts` for pairwise READY-candidate conflicts. The
scheduler continues to own dependency, priority, fairness, worker-limit, and
capability decisions. A launch recommendation is not a lock: the runtime must
successfully acquire the task's lease before changing it to `RUNNING`.

## Normalized matching

Filesystem targets are resolved without requiring them to exist. Relative
targets are anchored at the canonical project root. Environment variables and
glob metacharacters are never expanded by the shell.

| Kind | Normalized identity and matching rule |
| --- | --- |
| `path` | Absolute resolved path. It matches the same path, a containing `directory`, or a `glob` that matches the path. |
| `directory` | Absolute resolved path. Equal or nested directories overlap; it also overlaps contained paths. Against a glob, overlap is conservatively decided from the glob's literal root. |
| `glob` | Absolute pattern with a resolved non-magic prefix. `*`, `?`, character classes, and component-level `**` are supported when matching a path. Two globs with nested literal roots overlap, deliberately failing closed rather than attempting arbitrary glob-language intersection. |
| `application` | NFKC Unicode normalization, collapsed whitespace, and Unicode case-folding; exact target match within this kind. |
| `environment` | Same named-resource normalization and exact same-kind target match. Use for a named deployment/runtime environment, not an environment-variable expression. |
| `browser` | Same named-resource normalization and exact same-kind target match. |
| `device` | Same named-resource normalization and exact same-kind target match. |
| `external_sandbox` | Same named-resource normalization and exact same-kind target match. |
| `logical` | Same named-resource normalization and exact same-kind target match. Use for non-filesystem invariants such as `git:index`, a release version, or a single-writer migration. |

Named identities never alias across kinds: for example, `application:chrome`
and `browser:chrome` are different resources unless the plan declares a shared
logical claim as well.

After two claims match, access conflicts are symmetric:

| Held/requested | `read` | `write` | `exclusive` |
| --- | ---: | ---: | ---: |
| `read` | allowed | conflict | conflict |
| `write` | conflict | conflict | conflict |
| `exclusive` | conflict | conflict | conflict |

Thus multiple readers can share a resource. `exclusive` is the explicit
non-mergeable mode and blocks every matching claim, including readers.

## Ownership and journal

Every held lease contains a unique ownership token, run ID, task ID, attempt,
worker ID, optional thread/turn IDs, normalized claim snapshot, acquisition and
heartbeat timestamps, and an optional Computer Use slot number. One task may
have only one held lease. A repeated acquisition with the identical token and
metadata is idempotent; a different owner cannot acquire the same task, even
when its resource list is empty. The attempt must match durable task state.

`run-state.json` stores the current leases plus a contiguous append-only
acquisition/release journal. Each release must reproduce the token, task,
claim IDs, and slot from the active acquisition. Loading replays the journal
and requires its projected active leases to equal the stored lock snapshot. It
also rejects duplicate owners or slots and any pair of persisted conflicting
leases. Atomic state replacement means the snapshot and journal advance
together.

Heartbeats are liveness hints, not unlock authority. After a crash or restart,
reconciliation uses an authoritative worker-state lookup keyed by ownership
token:

- `active`: retain the lease;
- `terminal` or authoritatively `absent`: journal a reconciled release and free
  the lease;
- `unknown` or a missing answer: retain the lease and report it unresolved.

Wall-clock age alone never releases a lease. This makes recovery fail closed:
an unavailable control plane may delay work, but it cannot admit a duplicate
writer. Once authoritative absence is known, the reconciled release prevents a
permanent deadlock and a waiting owner may acquire normally.

## Computer Use slots

`computer_use_slots` defaults to `1` in plans, config, and durable state. A
Computer Use task receives the lowest available slot when its resource bundle
is acquired. Slot capacity is checked independently from filesystem and named
claims. Code workers never consume a Computer Use slot, so a held GUI slot does
not block unrelated code work. A code and a GUI task can still conflict when
they explicitly claim the same underlying resource.

## Shared-working-tree strategy

Workers use one canonical Git working tree; v0.9 does not merge speculative
per-worker branches. A task that may mutate files must declare every possible
write region with `path`, `directory`, or a suitably bounded `glob`. Disjoint
write regions can run concurrently. Shared files, generators with broad
outputs, the Git index, release metadata, and other non-mergeable state must be
declared with `exclusive` access (often as a `logical` identity).

An empty claim list means the task promises that it needs no exclusive or
mutable shared resource; it is not an automatically inferred project-wide
write permission. Verification should reject a plan whose declared claims do
not cover its intended writes.

`git.auto_commit=false` remains the default and is fully compatible with this
strategy: the coordinator never commits, stashes, resets, or discards changes,
and completed disjoint edits remain together in the user's working tree. If a
future runtime executes an explicitly enabled automatic commit concurrently,
that operation must acquire an exclusive non-mergeable `logical:git:index`
claim in addition to its file claims.

The Desktop lifecycle acquires the lease in the same atomic replacement that
changes READY to RUNNING and persists the reservation/launch descriptor. It
preserves ownership through ambiguous App Server create or production-turn outcomes, reconciles
against authoritative Desktop Stop/Interrupt identities, and releases only
after an authoritative terminal result. The explicit headless compatibility
runner does not weaken this Desktop-owned invariant.
