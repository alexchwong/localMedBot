# Functional verification policy

Tests assert behaviour, structured relationships and observable outcomes. Verbatim-content comparisons are allowed only for **fixture inputs**, including HTTP credential sentinels and source-passage integrity. Production prompt wording, README text, generated prose, HTML snapshots and error sentences are not protected by tests.

Recorded responses are injected model inputs. The assertion is what the runtime does with them: preserve a source relationship, block an invented ID, exhaust a retry limit, invalidate a revision or accept a valid approval. Protocol enums and schema field types are functional contracts.

## Commands

```bash
python -m unittest discover -s tests -v
```

For real browser tests:

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

`LOCALMEDBOT_BROWSER_EXECUTABLE` can select an already installed Chromium. `LOCALMEDBOT_SCREENSHOT_DIR` optionally saves visual QA images; screenshots are not golden test expectations. CI has separate core and Chromium jobs, neither requiring a paid model.

## Coverage by behaviour

| Test group | Behaviour |
| --- | --- |
| `CompilerTests` | Dependencies, missing producers, mandatory gates, schema changes and registered subclass execution |
| `KnowledgeTests` | Atomic/idempotent import, new revisions, typed filtering, empty retrieval, source locators, case scope and overlays |
| `Workflows` | All seven examples, approval, invalidation, repair, bounded revision, tool denial, evidence envelopes, crash/resume and frozen assets |
| `AdditionalBoundaries` | Arbitrary index queries, model ingestion, unconditional review gates, time limits and malformed query arguments |
| `HTTPTests` | Real local HTTP transport with fixture responses, authentication headers, retry, timeout and secret exclusion |
| `WebTests` | API actions, source access, stale review, host/origin/token enforcement, upload limits and recorded-mode boundaries |
| `CLITests` | Command execution, approval and error exit codes |
| `BrowserTests` | Chromium submission, source inspection, revision, approval, stale approval, import, unresolved answers and escaped content |

## Live model acceptance

HTTP fixture tests validate protocol behaviour, not model inference. Live acceptance must separately execute both workflows against real model endpoints and review source fidelity, applicability, omissions and unsupported assertions. At least one local and one hosted configuration must be exercised across that acceptance set.

The environment used to build this repository had no configured model credentials or endpoint, and no local server on the usual configured ports. This gate is explicitly blocked, not reported as passed. After providing a model, run both examples with `--config`, inspect their source/check artifacts and record the result in `docs/verification.md`.

Do not require exact prose. Do not use an LLM reviewer as the sole correctness oracle. Successful synthetic cases do not validate clinical deployment.
