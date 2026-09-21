# localMedBot

localMedBot is a localhost clinical-workflow prototype with bounded model reasoning, source-linked artifacts, explicit checks, and human review.

The repository currently provides two workflows:

- **Clinical letter** — paste free-text clinical notes and a communication purpose; extract source-linked facts, independently check them, draft one claim per included fact, recheck, render, and review.
- **Guideline QA** — select an independently versioned guideline set, ask a free-text question, use bounded lexical search/read reasoning, assess evidence, preserve conflicting guideline alternatives, render citations, and review.

The authoritative product version and release permission live in `src/localmedbot/version.json`. See `docs/versioning.md`.

## Setup

Requires Python 3.10+ and a browser. Repository Python commands must run through the checkout's `.env` environment.

POSIX:

```bash
python3 -m venv .env
source .env/bin/activate
.env/bin/python -m pip install -e '.[test]'
.env/bin/localmedbot check
.env/bin/localmedbot serve
```

Windows:

```powershell
py -3 -m venv .env
.env\Scripts\Activate.ps1
.env\Scripts\python.exe -m pip install -e ".[test]"
.env\Scripts\localmedbot.exe check
.env\Scripts\localmedbot.exe serve
```

`localmedbot check` compiles the bundled applications and reports `{"status": "valid", ...}`; `localmedbot serve` starts the single-user application on <http://127.0.0.1:8765> (`--port` changes it).

Asset and data locations default to the source-distribution layout and can be overridden per command: `--apps applications`, `--profiles model_profiles`, `--guidelines guideline_sets`, `--fixtures tests/fixtures/steps`, `--data .localmedbot`.

## Model profiles

Provider/model choices are workflow-specific **model profiles**, not workflow pipelines. Shipped profiles exist for OpenRouter, LM Studio, recorded demonstrations, and developer-only `self` execution.

The ordinary UI can configure OpenRouter or LM Studio endpoint/model and a process-memory credential, verify protocol compatibility, then run either workflow without editing JSON/YAML. Non-local/unknown execution is visibly warned because submitted text may leave the local domain.

OpenRouter defaults to `https://openrouter.ai/api/v1` and uses `OPENROUTER_API_KEY`. LM Studio defaults to `http://127.0.0.1:1234/v1`; an optional local-server token uses `LOCALMEDBOT_API_KEY`. No model is silently selected.

## Tests

Core deterministic verification:

```bash
.env/bin/python scripts/maintenance.py check
.env/bin/python -m unittest discover -s tests -v
```

Real-browser verification is opt-in:

```bash
.env/bin/python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 .env/bin/python -m unittest discover -s tests -p test_browser.py -v
```

Browser tests are gated on Flask, Playwright and `LOCALMEDBOT_BROWSER_TESTS=1`, so without the opt-in they report as skips. See `docs/testing.md` and the mandatory testing rule in `docs/devel-sysprompt.md`.

## Preview source package

The curated source ZIP is built only from committed Git blobs selected by `release-manifest.txt`:

```bash
.env/bin/python scripts/release.py validate
.env/bin/python scripts/release.py build --preview --output dist/localMedBot-preview.zip
.env/bin/python scripts/release.py verify --archive dist/localMedBot-preview.zip
.env/bin/python scripts/release.py smoke --archive dist/localMedBot-preview.zip
```

A successful preview is verification only; it is not publication permission. Release permission is controlled by the central lifecycle metadata and checked separately.

The supported distribution retains source-tree application, model-profile, guideline, configuration, test and documentation assets. Installing a Python wheel alone is **not** a complete runnable localMedBot distribution because those clinical/application assets are intentionally not relocated into site-packages. Run supported commands from the extracted source ZIP root or provide the existing explicit asset paths.

## Developer mode

Developer mode exposes self handoffs, resolved contracts, development guideline selection/import, and a one-step tester with scratch/captured fixtures. See `docs/DEVEL.md`, `docs/self-execution.md`, `docs/fixtures.md`, `docs/versioning.md`, and `docs/upgrade-0.1.0.md`.

Developer-only commands (`profiles import-legacy`, `guidelines import`/`promote`, `steps`, `fixtures`, `self`) require the `--developer` flag.

## Clinical and security limits

This remains a single-user localhost evaluation prototype, not an authenticated clinical sign-off system. Source linkage and model checks do not establish clinical accuracy. SQLite/artifact storage is not encrypted. Provider warnings are advisory; the operator is responsible for not sending patient data outside the intended domain. Retrieval is bounded lexical search, not an exhaustive literature review.

The current product is not releaseable. UI usability, end-to-end product acceptance and clinical validation remain unresolved. See `NEWS.md` and `docs/versioning.md` for lifecycle status.
