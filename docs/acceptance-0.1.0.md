# localMedBot 0.1.0 acceptance report

**Result:** implementation delivered with unmet DoD items caused by external environment prerequisites.

## Version and baseline

- Product version: **0.1.0**
- Workflow asset version: **2**
- Storage/run/step contract versions: **1 / 1 / 1**
- Reviewed repository baseline: `main` at `be2d8ee99c93768f210e04a0d31fe74d105f80aa`
- Specification: `tech_specs_0.1.0_v2.md`

## Verification completed

- `PYTHONPATH=src python -m unittest discover -s tests -v`: **58 tests, 0 failures, 0 errors, 9 environment-gated skips**. The 49 runnable tests all passed.
- `python -m compileall -q src scripts tests`: **pass**.
- `node --check src/localmedbot/static/app.js`: **pass**.
- Frontier `self` clinical-letter run `fb1d397bb4e54b56957665160741591d`: **waiting_review**, four model turns.
- Frontier `self` Guideline QA run `bc6f0f86581343f7a59794a9b30f300d`: **waiting_review**, five model turns and one runtime-owned `evidence.search` tool call.
- Isolated-step `self` verification: **all 9 model-dependent nodes completed**, using 10 self model calls total; Guideline QA `reason` used two self calls with one runtime-owned `evidence.search` between them. See `docs/self-step-verification-0.1.0.json`.
- OpenRouter live verifier: **blocked** with `credential_missing`; `OPENROUTER_API_KEY` is not present.
- LM Studio live verifier: **blocked** with `endpoint_unreachable`; no service is listening at `127.0.0.1:1234`.
- Browser/API execution: **blocked** in this container because Flask is not installed. Playwright is present, but the application server cannot import Flask.

No blocked live/browser item is reported as passed by substitute recorded inference.

## Definition of Done

| ID | Implemented | Verification | Evidence / remaining issue |
| --- | --- | --- | --- |
| D01 | yes | pass | Reviewed GitHub main is be2d8ee99c93768f210e04a0d31fe74d105f80aa; offline regression suite passes. |
| D02 | yes | pass | docs/development-instructions.md plus AGENTS.md, CLAUDE.md and .clinerules/01-localmedbot.md. |
| D03 | yes | pass | test_profiles_exist_and_filter; CLI `profiles list` shows eight templates. |
| D04 | yes | blocked | Offline implementation evidence passes where runnable. Browser execution could not be exercised because Flask is unavailable in this container. Browser execution could not be exercised because Flask is unavailable in this container. |
| D05 | yes | blocked | Offline implementation evidence passes where runnable. Real provider probes are externally blocked: OPENROUTER_API_KEY is missing and no LM Studio endpoint is reachable. Real provider probes are externally blocked: OPENROUTER_API_KEY is missing and no LM Studio endpoint is reachable. |
| D06 | yes | pass | test_credential_vault_not_persisted; test_http_pipeline_does_not_persist_secret. |
| D07 | yes | blocked | Offline implementation evidence passes where runnable. Locality classification passes offline, but required browser/API warning verification is blocked by unavailable Flask. Locality classification passes offline, but required browser/API warning verification is blocked by unavailable Flask. |
| D08 | yes | pass | Shared ModelStepService protocol tests plus `docs/self-step-verification-0.1.0.json`: all nine model-dependent nodes completed through workflow-specific self profiles; reasoning used a runtime-owned search between self calls. |
| D09 | yes | pass | test_source_version_and_contract_versions_frozen and profile/corpus mutation tests. |
| D10 | yes | pass | self duplicate/replay tests, budget tests, restart/external-response contracts and writer/cancellation semantics. |
| D11 | yes | blocked | Offline implementation evidence passes where runnable. Implementation and recorded/self execution pass, but required real-browser full workflow and live HTTP letter case are externally blocked. Implementation and recorded/self execution pass, but required real-browser full workflow and live HTTP letter case are externally blocked. |
| D12 | yes | blocked | Offline implementation evidence passes where runnable. Behavior fixtures and frontier self case pass; provider-specific live semantic rubric remains blocked by unavailable OpenRouter/LM Studio. Behavior fixtures and frontier self case pass; provider-specific live semantic rubric remains blocked by unavailable OpenRouter/LM Studio. |
| D13 | yes | pass | test_revision_invalidates_descendants_not_ancestors; stale omission/review handling tests. |
| D14 | yes | blocked | Offline implementation evidence passes where runnable. Core negative approval tests pass; required browser negative verification is blocked by unavailable Flask. Core negative approval tests pass; required browser negative verification is blocked by unavailable Flask. |
| D15 | yes | pass | test_guideline_scope_is_single_set plus demo/demo_alt colliding-scope checks. |
| D16 | yes | pass | test_devel_is_frozen_and_promotion_independent. |
| D17 | yes | blocked | Offline implementation evidence passes where runnable. Recorded/self QA behavior passes; required browser and live HTTP QA cases are externally blocked. Recorded/self QA behavior passes; required browser and live HTTP QA cases are externally blocked. |
| D18 | yes | blocked | Offline implementation evidence passes where runnable. Conflict fixtures pass; required real-provider live conflict inspection is externally blocked. Conflict fixtures pass; required real-provider live conflict inspection is externally blocked. |
| D19 | yes | pass | Full frontier self workflows reached `waiting_review` (clinical 4 turns; QA 5 turns with runtime search), and `docs/self-step-verification-0.1.0.json` records isolated self completion for all nine model-dependent nodes. |
| D20 | yes | blocked | Offline implementation evidence passes where runnable. CLI/service developer gating passes; required browser access-control verification is blocked by unavailable Flask. CLI/service developer gating passes; required browser access-control verification is blocked by unavailable Flask. |
| D21 | yes | blocked | `docs/self-step-verification-0.1.0.json`: all nine isolated model-dependent nodes complete through self; `reason` used 2 self calls + 1 runtime search. The specification additionally requires representative real OpenRouter/LM Studio isolated-step acceptance, which remains externally blocked. |
| D22 | yes | blocked | Offline implementation evidence passes where runnable. Capture/scratch/repository fixture behavior passes offline; required UI interaction evidence is blocked by unavailable Flask. Capture/scratch/repository fixture behavior passes offline; required UI interaction evidence is blocked by unavailable Flask. |
| D23 | yes | pass | test_fixture_runs_in_fresh_store_after_source_run_deleted. |
| D24 | yes | blocked | Offline implementation evidence passes where runnable. Filesystem/promotion negatives pass offline; required API negative evidence is blocked by unavailable Flask. Filesystem/promotion negatives pass offline; required API negative evidence is blocked by unavailable Flask. |
| D25 | yes | pass | test_legacy_backup_and_read_only_marker; test_run_delete_removes_private_sources_not_shared_guidelines; writer-guard test. |
| D26 | yes | blocked | Offline implementation evidence passes where runnable. Renderer/provenance state tests pass offline; required browser-state assertions are blocked by unavailable Flask. Renderer/provenance state tests pass offline; required browser-state assertions are blocked by unavailable Flask. |
| D27 | yes | blocked | Offline implementation evidence passes where runnable. OPENROUTER_API_KEY is not present, so actual OpenRouter workflow, isolated-step, and browser acceptance cannot run. OPENROUTER_API_KEY is not present, so actual OpenRouter workflow, isolated-step, and browser acceptance cannot run. |
| D28 | yes | blocked | Offline implementation evidence passes where runnable. No LM Studio server is reachable at 127.0.0.1:1234, so actual LM Studio workflow, isolated-step, and browser acceptance cannot run. No LM Studio server is reachable at 127.0.0.1:1234, so actual LM Studio workflow, isolated-step, and browser acceptance cannot run. |
| D29 | yes | blocked | Offline suite passes 58 tests with 49 runnable passes and 9 browser/API environment skips; browser/API completion remains blocked because Flask is unavailable. |
| D30 | yes | pass | README.md; docs/versioning.md; docs/upgrade-0.1.0.md; docs/self-execution.md; docs/fixtures.md; this acceptance report. |
| D31 | yes | blocked | Offline implementation evidence passes where runnable. C01-C05 and C07-C10 contracts have runnable offline evidence; browser-session/API portions of C06/C11 are blocked by unavailable Flask. C01-C05 and C07-C10 contracts have runnable offline evidence; browser-session/API portions of C06/C11 are blocked by unavailable Flask. |

## Unmet DoD items

D04, D05, D07, D11, D12, D14, D17, D18, D20, D21, D22, D24, D26, D27, D28, D29, D31.

These are verification blockers, not permissions to weaken the acceptance criteria. The implementation paths and deterministic/protocol tests remain present.

## External prerequisites and closing commands

### Browser suite

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Successful closure requires the browser/API tests to run rather than skip, with no failures.

### OpenRouter

```bash
export OPENROUTER_API_KEY='<secret>'
PYTHONPATH=src python scripts/verify_live_provider.py \
  --provider openrouter --workflow both --model '<model-id>' \
  --report .localmedbot/openrouter-0.1.0.json
```

Successful closure requires report status `pass`, both versioned synthetic workflows reaching `waiting_review`, representative isolated steps completing, and the actual browser provider configuration/run path being exercised.

### LM Studio

Start LM Studio with an OpenAI-compatible `/v1` server and a loaded model, then:

```bash
PYTHONPATH=src python scripts/verify_live_provider.py \
  --provider lmstudio --workflow both --model '<loaded-model-id>' \
  --base-url http://127.0.0.1:1234/v1 \
  --report .localmedbot/lmstudio-0.1.0.json
```

Successful closure is the same as OpenRouter, with the local endpoint classified and displayed correctly.

## Delivery notes

- Repository fixture/source tests use version-controlled synthetic material; no production prompt/guideline prose is protected by verbatim snapshot assertions.
- `applications/guideline_qa/corpus.json` is superseded by `guideline_sets/demo/releases/v1/` and should be removed when applying the changed-files archive to the reviewed baseline.
- The archive intentionally excludes local run databases, writer locks, caches, credentials and generated browser artifacts.
