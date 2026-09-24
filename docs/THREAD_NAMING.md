# Deterministic thread naming

The v0.9 contract requires concise titles derived only from structured task
metadata:

```text
<Role> | <Task ID> | <Short Task Title>
<Lead Role> | Verify <Task ID> | <Short Task Title>
<Role> | <Task ID>-R<revision> | <Short Revision Title>
Planner | PLAN | <Short Project Goal>
Planner | PC-<ID> | <Short Change Purpose>
Screening | Hire <Task ID> | <Short Task Title>
```

Every verifier is its department's lead (R30), so its title names the lead:
`Character Art Verifier | Verify M01 | Model part M01`. The older
`<Verifier Role> Verifier | <Task ID> | ...` form is no longer produced.

`Screening` appears only in a run that has turned hiring on
(`runtime.skill_screening`); it is the thread that decides which skills the
task's worker will carry, and it runs before that worker exists.

For the reference task, the exact titles are:

```text
3D Artist | T44 | Create Weapon Model
Art Lead | Verify T44 | Create Weapon Model
3D Artist | T44-R1 | Revise Weapon Model
```

Project names, timestamps, UUIDs, and generated filler do not belong in the
user-visible title. Internal thread IDs remain separate metadata. The runtime
must set the title before production and read it back through App Server/Desktop
metadata; a desired-title field alone is not evidence.

## Names before threads (staffing)

The run's roster (`staffing.py`, `.codex-autopilot/roster.json`) carries, for
every task, the names its worker's and its lead's threads will have, made
before the first task. The threads themselves are not made in advance: App
Server does not keep a thread with no turn (four empty probe threads vanished
from `thread/list`), threads created ahead ("slots", `worker_thread_ids`)
were removed for want of an ownership hand-over and for vanishing after two
idle hours, and R30 and rehiring want a fresh session per acceptance and per
revision. The dispatcher still starts each thread 10-60 ms before its turn.

What the owner wanted from the sidebar is the branch board instead:
`codex-autopilot status` and the status card print one line per task -
department, lead and rubric version, its state in the run's language
(working and in which thread, waiting for which dependencies, waiting for or
under acceptance, revision N of M with hire and effort, stopped with the
on-call's ticket, waiting for her with the decision, the recommendation and
the ready `codex-autopilot unblock` command, accepted), the first line of its
last report and its threads - under one summary line with what staffing
found (the roster, the isolation the dispatcher will use, the roots audit - as
they stand, not as the bootstrap saw them). A task stopped without an open
ticket shows its own stop's reason, journaled by the door with the tasks it
holds, else its session's failure - never the run's last error, which the
next stop of another task overwrites. The runtime rewrites
`.codex-autopilot/BOARD.md` at every save of run state and after a new
isolation record; a failure to write it never stops a run.

Next step, by measurement only: whether a task's thread lives on after one
completed turn and accepts `turn/start` from another process - the condition
for a permanent task branch as its report channel. Not assumed here; it needs
a live App Server measurement.

## Candidate status

`src/codex_autopilot/thread_titles.py` currently emits middle-dot titles such as
`3D Artist · Implement T44 · Create Weapon Model`, does not append `Verifier` to
the verifier role, and uses `Plan`/`Replan` prefixes. Existing unit tests assert
that non-conforming shape. The independent contract regression in
`tests/test_v09_acceptance_contract.py` fails until the implementation and its
older tests are revised. Actual Desktop sidebar titles were not live-tested in
M10.
