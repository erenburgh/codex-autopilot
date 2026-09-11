# Testing v0.9

Run the deterministic unit suite from the repository root:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run only the independent v0.9 contract and synthetic-shape checks with:

```text
PYTHONPATH=src python3 -m unittest -v tests/test_v09_acceptance_contract.py
```

The M10 audit first ran the pre-existing suite: 260 tests passed and four
documented legacy App Server transport cases were skipped. It then added eight
independent acceptance tests. The three AI Studio structure tests pass:

1. independent backend/frontend branches followed by integration;
2. research, analysis, then fact verification;
3. code work continuing while two Computer Use tasks serialize, with a
   Computer Use verifier route.

Five contract regressions currently fail or error: default execution strategy,
default worker surface, exact thread-title shape, unrelated initiating-project
fallback, and deterministic verification promotion. Those failures are
intentional release evidence, not quarantined skips. The full suite is therefore
expected to remain red until the substantive runtime revision is made.

Tests with fake App Server/Desktop clients prove payloads and fail-closed logic;
they are deterministic coverage, not live UI evidence. The developer-only
`scripts/live_acceptance.py` still exercises the legacy serial orchestrator and
does not constitute v0.9 parallel AI Studio acceptance. M10 did not run
production through App Server, so live Sol/Astra overlap, Desktop sidebar title
readback, Desktop project grouping, and real Computer Use serialization remain
NOT TESTED.

See `RELEASE_VERIFICATION_0.9.0-beta.md` for the evidence matrix and unresolved
revision items.
