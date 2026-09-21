# localMedBot 0.1.0

localMedBot is a localhost clinical-workflow prototype with bounded model reasoning, source-linked artifacts, explicit checks, and human review.

Version 0.1.0 provides two workflows:

- **Clinical letter** — paste free-text clinical notes and a communication purpose; extract source-linked facts, independently check them, draft one claim per included fact, recheck, render, and review.
- **Guideline QA** — select an independently versioned guideline set, ask a free-text question, use bounded lexical search/read reasoning, assess evidence, preserve conflicting guideline alternatives, render citations, and review.

## Setup

Requires Python 3.10+ and a browser. Run from this repository's root:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e .
localmedbot check
localmedbot serve
```

`localmedbot check` compiles both applications and reports `{"status": "valid", ...}`; `localmedbot serve` starts the single-user application on <http://127.0.0.1:8765> (`--port` changes it). Virtual environments and run state are excluded from git (`.venv/`, `.localmedbot/`).

Asset and data locations default to the repository layout and can be overridden per command: `--apps applications`, `--profiles model_profiles`, `--guidelines guideline_sets`, `--fixtures tests/fixtures/steps`, `--data .localmedbot`.

## Model profiles

Provider/model choices are workflow-specific **model profiles**, not workflow pipelines. Shipped profiles exist for OpenRouter, LM Studio, recorded demonstrations, and developer-only `self` execution.

The ordinary UI can configure OpenRouter or LM Studio endpoint/model and a process-memory credential, verify protocol compatibility, then run either workflow without editing JSON/YAML. Non-local/unknown execution is visibly warned because submitted text may leave the local domain.

OpenRouter defaults to `https://openrouter.ai/api/v1` and uses `OPENROUTER_API_KEY`. LM Studio defaults to `http://127.0.0.1:1234/v1`; an optional local-server token uses `LOCALMEDBOT_API_KEY`. No model is silently selected.

## Tests

The deterministic suite needs no model and no browser:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v   # uninstalled checkout
python -m unittest discover -s tests -v                  # after `pip install -e .`
```

Real-browser tests are opt-in and use the existing Playwright extra:

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium          # add --with-deps on Linux/CI
LOCALMEDBOT_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Run one module with `python -m unittest discover -s tests -p 'test_core_v2.py' -v`. Browser tests are gated on Flask, Playwright and `LOCALMEDBOT_BROWSER_TESTS=1`, so without them they report as **skips rather than failures** — read the skip count, not just the exit status. Both cases are mirrored in `.github/workflows/tests.yml`. See `docs/testing.md` and the verbatim-content rule in `docs/development-instructions.md`.

## Developer mode

Developer mode exposes self handoffs, resolved contracts, development guideline selection/import, and a one-step tester with scratch/captured fixtures. See:

- `docs/self-execution.md`
- `docs/fixtures.md`
- `docs/versioning.md`
- `docs/upgrade-0.1.0.md`
- `docs/development-instructions.md`

Developer-only commands (`profiles import-legacy`, `guidelines import`/`promote`, `steps`, `fixtures`, `self`) require the `--developer` flag.

## Clinical and security limits

This remains a single-user localhost evaluation prototype, not an authenticated clinical sign-off system. Source linkage and model checks do not establish clinical accuracy. SQLite/artifact storage is not encrypted. Provider warnings are advisory; the operator is responsible for not sending patient data outside the intended domain. Retrieval is bounded lexical search, not an exhaustive literature review.
