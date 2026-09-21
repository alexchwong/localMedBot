# Testing

Repository Python commands run through the checkout's `.env`. Core deterministic verification is:

```bash
.env/bin/python scripts/maintenance.py check
.env/bin/python -m unittest discover -s tests -v
```

Real-browser verification uses the existing Playwright extra when available:

```bash
.env/bin/python -m pip install -e '.[test]'
.env/bin/python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 .env/bin/python -m unittest discover -s tests -p test_browser.py -v
```

Tests assert state transitions, schemas, references, idempotency, scope, package integrity and other observable contracts rather than production prose. See `docs/devel-sysprompt.md` for the mandatory verbatim-content rule.

Fixture dependencies are declared, not inferred. A test whose assertions depend on a repository fixture carries `@fixture_dependency(...)` from `tests/support.py`, naming each repository-relative fixture path; the declaration is recorded on the test and appended to its description, so `unittest -v` output shows which fixtures a test depends on. `tests/test_fixture_policy.py` enforces that every declared path is an existing tracked asset below `tests/fixtures/`, that the declared fixture's identity is used by the test that declares it, that test functions referencing repository fixture *files* by path carry a declaration, and that no test restates the product version label as a constant. The path scan is best-effort by design: fixtures loaded through the runtime by immutable id and version leave no path literal to scan, so review carries that part of the obligation.

Fake local HTTP tests validate protocol behaviour only. They do not establish live-model quality. OpenRouter and LM Studio acceptance must use real endpoints and versioned synthetic cases; a blocked endpoint remains an unmet acceptance item.

Developer `self` workflow verification continues through the existing orchestrator, invoked from `.env`:

```bash
.env/bin/python scripts/verify_self_workflow.py --action start --workflow guideline_qa
```

Live provider verification also remains separate from deterministic/package acceptance and must use a real configured endpoint:

```bash
.env/bin/python scripts/verify_live_provider.py --provider lmstudio --workflow both \
  --report .localmedbot-live-acceptance/report.json
```

A blocked credential or endpoint is reported as blocked, not converted into a recorded-model pass.

The curated package smoke test is separate from browser/live acceptance. It verifies exact committed ZIP contents, isolated non-editable installation, bundled application compilation and the deterministic recorded Guideline QA workflow. See `docs/DEVEL.md`.
