# M11 implementation checkpoint — 2026-09-12

M11 is **incomplete**. This checkpoint requests a bounded resource-contract
change under R7. It is neither release acceptance nor an independent verdict.

## Inspected state

The canonical checkout is on `repair/m10-p0` at
`67289d3b1d341a3f7d3949e3ee252efec0d65c09`. The starting dirty paths were
`ROADMAP.md`, `docs/RELEASE_VERIFICATION_0.9.0-beta.md`, and the untracked
`scripts/promote_thread_visibility.py`. Their bytes were preserved.

During verification, HEAD advanced outside M11's command sequence to
`293d319d06faae45703e9e5aa31a630a474c6ef7`, including this checkpoint and
additional placement-gate source/tests. M11 neither created nor reverted that
commit. The new diff was inspected and retained. M11's own source changes are
limited to `plan.py` and two regressions in `tests/test_plan_evolution.py`;
no plugin, installer, or live harness was edited.

The structured M11 task, its reservation, source files, and freshly executed
checks were inspected. The M10 issue list in section 11.6 of the independent
audit was used to identify requested work, not as evidence that repairs pass.
No worker transcript or historical handoff was used.

## New reproducible evidence

| Check ID / evidence role | Project Memory ID | Observed result |
| --- | --- | --- |
| `M11-BASELINE-TESTS` | `EVID-071` | 381 tests in 7.317 seconds; exit 0; 4 skips; no failures/errors. Process wall time was 7.407 seconds. This is the pre-change deterministic baseline. |
| `M11-CONTRACT-SCOPE` | `EVID-072` | Production `audit_declared_scope()` rejects all seven required paths below for the current M11 task. The probe exits 2 with `PLAN_CHANGE_REQUIRED`. |
| `M11-LIVE-AUTHORIZATION` | `EVID-073` | The current user instruction prohibits this worker from creating, starting, or messaging other tasks. This is a task-contract boundary, not an observed App Server limitation. |
| `M11-MIGRATED-REPLAN-BEFORE` | `EVID-074` | The new production lifecycle regression failed before the guard repair: 2 tests, 1 error. |
| `M11-MIGRATED-REPLAN-AFTER` | `EVID-075` | All 26 plan-evolution/task-graph checks passed after the repair. |
| `M11-STABLE-TESTS` | `EVID-076` | 388 tests in 7.492 seconds; exit 0; 4 skips. HEAD `293d319` and 126 source-file hashes were unchanged during the run; process wall time 7.589 seconds. |
| `M11-CHECKPOINT-VALIDATION` | `EVID-077` | The resource request parses and an additive candidate passes the production schema/DAG validator; initial dirty-file bytes are preserved. This is lint, not acceptance of the plan's meaning. |
| `M11-MIGRATED-REPLAN-PATCH` | `EVID-078` | Exact M11 source/test patch, SHA-256 `9f5973fd8e3a58a0444ca1a331d0f7c1160257baec4b6625d47984018860e6a1`. |

Exact deterministic commands:

```sh
env -u CODEX_THREAD_ID -u CODEX_TURN_ID -u CODEX_SESSION_ID PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 dist/m11-evidence-c4cd11ab/scope_probe.py
python3 dist/m11-evidence-c4cd11ab/run_stable_tests.py
```

The first command's final output was:

```text
Ran 381 tests in 7.317s

OK (skipped=4)
```

The second command reads the current M11 task and calls the production scope
checker without mutating the plan. It also directly parses the checked-out
skills, manifests, installer, builder, and legacy live harness. Both skill
examples explicitly select `serial` and one worker. The package metadata is
`0.8.0b0`; the package, installer, builder, and host plugin say `0.8.0-beta`;
the adaptive plugin says `0.8.0-beta+codex.20260911012326`. The live harness
imports the legacy `DesktopOrchestrator`, and the builder's source exclusion
set does not include `.codex-autopilot`.

Full logs, the reproducible probe, initial file hashes, and MCP receipts are in
`dist/m11-evidence-c4cd11ab/`. The baseline log SHA-256 is
`1301b9bc232f2069a1fcf76f37566075a2170ce2416beca0f8bb8e3986e58395`.
The scope-probe log SHA-256 is
`7f0c3118c16a8939a56c8e4b96d762e70fd7db451e95b0a105e9f68a982c1bb9`.
These are evidence-file checksums, not release-artifact checksums.
The post-repair snapshot and complete test output are in `stable-tests.json`;
its SHA-256 is
`b0d45105cad8677f5c5ba95972c54c6f5b38eb4d538442f19ea870d1d33da890`.
This report's result rows were added after that test snapshot; no production
source or test changed after the stable run.

## Required resource additions

The existing M11 resources cover `src`, `tests`, `docs`, `dist`, package
metadata, and three top-level documents. They do not cover these production
entrypoints. Preserve every existing claim and add write claims for:

| Path | Required work |
| --- | --- |
| `plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md` | Close `M11-ENTRYPOINT-DEFAULTS`; align the worker checkpoint and R29/R30 acceptance instructions with the repaired runtime. |
| `plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md` | Align the shipped second profile's task and acceptance instructions while preserving host routing and migrated serial behavior. |
| `plugins/codex-autopilot-adaptive/.codex-plugin/plugin.json` | Set coherent v0.9 release metadata. |
| `plugins/codex-autopilot-host-settings/.codex-plugin/plugin.json` | Set coherent v0.9 release metadata. |
| `install.sh` | Install into the v0.9 version directory; verify additive upgrade and recoverable replacement semantics. |
| `scripts/build_release.py` | Produce v0.9 artifacts reproducibly and exclude private runtime state from source staging. |
| `scripts/live_acceptance.py` | Replace the legacy acceptance entrypoint with bounded v0.9 coverage without automatic production approvals. |

Editing these files under the present claims would violate R7. Changing only
`pyproject.toml` or repackaging overridden copies would leave the actual shipped
entrypoints inconsistent and would not close the audited issues. The proposed
resource change does not authorize this worker to launch another task or
change the canonical run state.

## Bounded migration repair

Validating the proposed resource extension exposed a separate production bug:
the canonical plan is schema 3 with
`compatibility={"migrated_from_schema":2,"legacy_serial":true}`. The previous
`validate_plan_change()` guard confused this preserved migration provenance
with the submitted plan's format and raised:

```text
ValueError: plan changes must use the canonical v0.9 schema
```

M11 changed that guard to check the submitted `schema_version`. It retains
migration provenance and the existing serial/one-worker compatibility checks.
The new lifecycle regression exercises resource request, fresh replanner
reservation, replanner completion, durable graph application, and preserved
serial settings using deterministic test doubles. It failed before the fix.
A second regression confirms that an actual schema-2 replacement is still
rejected. All 26 plan-evolution/task-graph tests then passed in 0.249 seconds.
The canonical plan/state and the installed active runtime were not rewritten.

This is a source repair with deterministic evidence; it is not a live
replanner test or independent acceptance. The original failure log is retained
as `migration-regression-before.log` in the evidence directory.

## Remaining acceptance work

The section-11.6 issues still require implementation or fresh acceptance evidence:
the prompt-time retired-session fence; creation-causality reachability;
Desktop membership/editability and timeout incidents; root drift fail-closed
behavior; task attribution and budgets; closed escalation reasons; structured
phase contracts; mandatory external-input provenance; real recovery actions
and healthchecks; and department-derived fresh leads with stable rubrics.
Their prior audit claims were not promoted to Truth in this attempt.

All requested new live multi-worker scenarios are **NOT TESTED** here,
including Sol overlap, Sol+Astra overlap, Computer Use serialization,
rejection/revision/unlock, multi-worker pause/crash recovery, and actual
Desktop placement/title/readability checks. No supported-runtime impossibility
has been established. The explicit task restriction is not treated as the
DoD's exception for a proven product/runtime limitation.

After the contract is reconciled, repair the audited production paths, rerun
the relevant regressions and complete suite, and establish the permitted
dispatcher-owned live-test path before executing those scenarios. Then finish
versioning, migration and product documentation, two-build reproducibility,
contamination scans, isolated installation checks, and the requested 57-item
release report. No release ZIP, v0.9 installation, or release SHA-256 is claimed
by this checkpoint. Fresh independent acceptance remains mandatory under R29
and R30.

No commit, tag, push, publication, reset, cleanup, other-task launch, active-task
control operation, Codex UI interaction, or permission change was performed.
The mandatory task checkpoint is `.codex-autopilot/handoff/M11.md`.

Applied rule IDs: R2, R4, R7, R8, R9, R11, R16, R17, R19, R21, R22, R25, R26, R27,
R29, R30.
