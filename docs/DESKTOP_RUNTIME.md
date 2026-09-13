# Desktop-owned runtime

## Supported ownership boundary

`desktop_owned` is the only execution surface; `headless_app_server` was removed in 0.8.1 as unreachable.

In `desktop_owned`, deterministic local code computes the READY frontier,
reserves it, acquires resource locks, persists launch descriptors, validates
completion, advances dependencies, and schedules retries. Under the user's
durable authorization for the complete run, the exact causal predecessor's
Stop hook launches one detached local dispatcher. The dispatcher waits for that
predecessor turn to be durably complete, performs App Server `thread/start` with
the canonical cwd and App Server project ID, verifies title/cwd/project
metadata, performs production `turn/start`, waits for completion, and exits.
Codex App `create_thread` and `send_message_to_thread` are not used. Hook output
is informational and is never relied on to continue a model turn.

The official App Server contract does **not** provide an immediate ownership
handoff primitive. [`thread/unsubscribe`](https://learn.chatgpt.com/docs/app-server)
removes only the calling connection's subscription. When it removes the last
subscriber, App Server may keep the thread loaded until there have been no
subscribers or activity for 30 minutes; only then is `thread/closed` emitted.
The generated 0.153.4 schema likewise exposes only `notLoaded`,
`notSubscribed`, and `unsubscribed` response statuses—not a synchronous
`unloaded` or “Desktop owns writer” acknowledgement.

Local v0.8 logs corroborated that boundary: a separate stdio client received
`already has an active writer` while trying to resume visible tasks, and a
successful production turn followed by an `unsubscribed` response did not
prove immediate unload. Consequently, unsubscribe is cleanup in headless mode,
never a Desktop ownership guarantee.

The repair therefore does not use a Desktop-created slot, no-op turn,
archive/unarchive, or `thread/unsubscribe` as a transfer protocol. The App
Server dispatcher owns the production turn until it completes and the process
exits. That exit does not by itself prove Desktop visibility or editability;
those remain separate live observations. Until required observations exist,
the run remains fail-closed and no replacement task is created.

App Server is used for bounded preflight, metadata/trust probes, and automatic
worker creation/execution. Each worker process exits after its turn. The runtime
never treats `thread/unsubscribe` as an ownership barrier.

Before any production task is reserved, the trusted lifecycle hook calls the
supported `hooks/list` method through a bounded App Server process. The
inventory must contain exactly one enabled command hook for the selected
Autopilot plugin's `Stop` event, report no errors, have `trustStatus` `trusted`
or `managed`, and match the stable installed `current/bin/codex-autopilot` command. `modified` or
`untrusted` reports `APPROVAL REQUIRED` and asks for one normal review in
`/hooks`; every other mismatch fails closed. Only after that gate succeeds is
the one-shot reservation written. The reservation records the exact source task
as `relay_owner_thread_id`. That identity is immutable and is the only task
allowed to own or retry the fixed automatic dispatch. Caller-supplied substitute
identities, unrelated tasks, and the destination worker ID fail closed.

Every production prompt prohibits manual task management. Workers never create,
fork, start, or message other tasks; the deterministic dispatcher alone applies
the already-authorized scheduler decision.

An explicit App Server `thread/start` error is a definitive no-side-effect
result. The runtime binds it to the exact claimed creation contract, records one
stable Pipeline Engineer incident, and schedules a causal retry without
executing it. A missing response or lost connection remains ambiguous. Neither
result becomes an automatic repeat. Legacy Codex App incidents remain readable
for migration, but new reservations never use that path.

## Canonical placement, names, and status

The canonical worker filesystem location is always the initialized target
repository root. Every App Server `thread/start` supplies that root directly as
`cwd`; there is no later model turn that tries to repair an initially wrong
checkout. The saved App Server `projectId` is resolved independently: an
explicitly configured matching project wins; otherwise Autopilot selects the
unique saved project with the longest root that contains the target. An
explicit mismatch or ambiguous longest-root match fails preflight.

Desktop and App Server project identifiers are separate namespaces. App Server
creation uses only the App Server ID. Codex App remains authoritative for
Desktop sidebar placement through the host's existing namespace mapping. When
`thread/read` exposes an App Server `projectId`, Autopilot records it and
verifies the configured App Server project exactly. It does not claim Desktop
association from that check: actual Desktop visibility/editability must be
observed independently.

Every worker title is deterministic, human-readable, and capped at 96
characters. The exact forms are:

- `<role> · Implement T44 · <task title>`
- `<verifier role> · Verify T44 · <task title>`
- `<role> · Revise T44-R1 · <task title>`
- `Plan · <goal summary>`
- `Replan PC7 · <change summary>`

Task-phase titles obtain `<role>` only from the task's validated `RoleProfile`;
the launch path has no phase-only fallback and never infers a role from task
prose. Canonical plans that assign the generic `legacy-worker` instead of a
concrete role fail validation. The generic title remains available only for a
truly role-less schema-2 compatibility plan.

The bounded create command applies the title through `thread/name/set`, then
reads the task back. A different or missing returned name fails creation
closed. Titles never include run
UUIDs, model labels, or redundant project prefixes.

Local status is semantic rather than a raw state dump. It reports the global
state (`Running`, `Verifying`, `Waiting`, `Ready`, `Blocked`, or `Done`), verified
progress, used and available worker slots, used and available Computer Use
slots, waiting reasons, canonical cwd, project-association evidence or its exact
limitation, and the exact active task titles. The same view shows
`Pipeline Engineer · On call`, its incident/recovery phase, exact affected task
IDs, recovery-slot owner, and pending authority-bound transports.

## Atomic JIT lifecycle

One filesystem transaction protects all deterministic admission decisions.
Within it, Autopilot:

1. reconciles due retries and dependency-eligible READY tasks;
2. selects no more than `max_parallel_workers - active_workers` while honoring
   resource conflicts and Computer Use capacity;
3. performs the READY → RUNNING transition;
4. allocates a stable reservation token and client operation identity;
5. acquires the complete resource bundle; and
6. embeds the full launch descriptor and `create_requested` journal record in
   the atomic `run-state.json` replacement.

Descriptor files under `.codex-autopilot/launches/` are derived conveniences;
the embedded descriptor is authoritative. The automatic dispatcher verifies
the exact causal owner while atomically changing `CREATE_REQUESTED` to
`RELAYING`. It then performs one `thread/start`; a second claim is refused.
A second scheduler invocation sees the RUNNING state and pending token and
cannot reserve the task again.

The journal carries `task_id`, attempt, reservation token, operation ID,
client-message identity, relay-owner identity, and nullable destination
thread/turn identities on every event.
The command validates the active permission profile and configured App Server
project before creation, then reads the created persistent thread back and
requires its exact ID, canonical cwd, deterministic title, and App Server
project ID. It records `app_server_thread_created` only after the ID is known,
and records `app_server_create_process_exited` only after the whole process has
terminated after production. An explicit `thread/start` error becomes one known-failed incident;
a lost response or any post-create metadata/exit uncertainty becomes
`AMBIGUOUS`, retains the resource lock, and cannot be retried or unlock a
dependency without authoritative reconciliation. App Server `turn/start` is a
one-shot claim; its returned turn ID is bound before completion can advance the
graph. The trusted [`Stop` hook](https://learn.chatgpt.com/docs/hooks) supplies the
authoritative production `session_id`, `turn_id`, and final assistant message;
it records `turn_identity_bound` and `turn_completed` before advancing the
graph. The `Interrupt` hook records the same identities and schedules only that
task for a deterministic retry.

If the platform moves an ACTIVE task and gives it a new thread identity, the
trusted hook or supported reconciliation command must supply the exact
reservation plus explicit previous and current IDs. One filesystem transaction
checks ACTIVE ownership and collisions, records `thread_identity_history`,
rebinds resource locks and `current_thread_id`, and journals
`thread_identity_reconciled`. The runtime never guesses this mapping from prose
or from the fact that only one worker happens to be active.

## Transport authority limitation

Command hooks update local state and spawn only the bounded automatic dispatcher;
they never invoke Codex App task APIs. The Stop hook does not ask the model to
continue. DevOps may repair transport and re-arm the exact owner token, but it
does not perform destination `thread/start` or `turn/start` itself. No recovery
actor substitutes for the recorded predecessor.

Verifier and revision tasks use this identical Desktop-owned boundary. They
receive distinct reservations and threads; verifier capability overrides also
participate in the durable Computer Use slot limit. Their phase-specific
prompts and results are described in
[Verification and revision lifecycle](VERIFICATION_LIFECYCLE.md).

An uncertain create or production-turn result becomes/remains `AMBIGUOUS` in
reconciliation. Autopilot retains its RUNNING state and resource lock and will
not repeat the uncertain side effect. A definite create failure may schedule a
new deterministic reservation, but its automatic dispatcher remains bound to
the exact original predecessor. A definite turn-start failure keeps the known
task and does not repeat transport automatically. After a crash, all
pending phases remain authoritative; the runtime never infers absence from
elapsed time and never unlocks dependencies without authoritative completion.

## Plan evolution, pause, and recovery

A typed worker `PLAN_CHANGE_REQUEST` stops ordinary admission and lets existing
workers drain with their locks intact. The runtime then launches exactly one
fresh replanner with the current graph and verified-state evidence selectors.
Only deterministic code validates and commits its complete next-version graph;
cycles, stale versions, changed verified contracts, and invalid references are
rejected before any canonical write.

Pause also uses drain semantics: it persists the pause marker, stops new
reservations, and leaves already-running Desktop turns and locks untouched.
Their authoritative Stop or Interrupt events may still be recorded. Resume
first completes any interrupted graph transaction and reconciles every running
ownership token; unknown workers remain locked, while authoritatively absent or
terminal workers enter `RETRY_WAIT` without advancing dependencies. Only then
is the pause cleared and fresh work admitted.

A provider reset timestamp creates a run-wide rate-limit admission barrier at
the later of bounded backoff or reset plus five seconds. Independent running
workers continue, but no new worker is created before the barrier expires.
Details and exact protocols are in
[Plan evolution and recovery](PLAN_EVOLUTION_AND_RECOVERY.md).

## Headless compatibility

`headless_app_server` preserves the serial v0.8 CLI runner and its source API
alias. It is intentionally named and documented as headless: production turns
are owned by the external App Server connection, and no Desktop follow-up,
steering, or immediate ownership-return promise is made. Desktop-owned hooks
reject attempts to spawn that dispatcher.
