# Deterministic thread naming

The v0.9 contract requires concise titles derived only from structured task
metadata:

```text
<Role> | <Task ID> | <Short Task Title>
<Verifier Role> Verifier | <Task ID> | <Short Verification Title>
<Role> | <Task ID>-R<revision> | <Short Revision Title>
Planner | PLAN | <Short Project Goal>
Planner | PC-<ID> | <Short Change Purpose>
```

For the reference task, the exact titles are:

```text
3D Artist | T44 | Create Weapon Model
3D Artist Verifier | T44 | Verify Weapon Model
3D Artist | T44-R1 | Revise Weapon Model
```

Project names, timestamps, UUIDs, and generated filler do not belong in the
user-visible title. Internal thread IDs remain separate metadata. The runtime
must set the title before production and read it back through App Server/Desktop
metadata; a desired-title field alone is not evidence.

## Candidate status

`src/codex_autopilot/thread_titles.py` currently emits middle-dot titles such as
`3D Artist · Implement T44 · Create Weapon Model`, does not append `Verifier` to
the verifier role, and uses `Plan`/`Replan` prefixes. Existing unit tests assert
that non-conforming shape. The independent contract regression in
`tests/test_v09_acceptance_contract.py` fails until the implementation and its
older tests are revised. Actual Desktop sidebar titles were not live-tested in
M10.
