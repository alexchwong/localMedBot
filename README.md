# localMedBot

localMedBot is a localhost clinical-workflow prototype with bounded model reasoning, source-linked artifacts, explicit checks, and human review.

The repository currently provides two workflows:

- **Clinical letter** — select a configured document purpose, paste clinical notes, extract and check source-linked facts, draft the complete communication with internal passage provenance, independently check it, and review it. Internal clinical-note references are not displayed as citations.
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

`localmedbot check` compiles the bundled applications and reports `{"status": "valid", ...}`; `localmedbot serve` binds the single-user application to loopback at <http://127.0.0.1:8765>, prints the URL and opens the default browser after binding. Use `--no-browser` to open it manually; `--port` changes the port. Successful browser traffic is not logged, but HTTP failures are reported.

Runtime locations are centralized and default relative to the launch directory: mutable shared state in `state/`, per-run immutable evidence in `runs/<run-id>/`, execution defaults in `config/`, reviewed fixtures in `tests/fixtures/`, and unreviewed scratch fixtures in `tests/fixtures/scratch/`. `--data` is retained as an alias for the shared state root; `--runs-root` and `--config-root` override the other runtime roots. An existing legacy `.localmedbot` is never silently ignored: run the explicit `localmedbot relocate` operation first.

## Model profiles

Provider/model choices are workflow-specific **model profiles**, not workflow pipelines. Shipped profiles exist for OpenRouter, LM Studio, recorded demonstrations, and developer-only `self` execution.

The ordinary UI can configure OpenRouter or LM Studio endpoint/model, reasoning level, and a process-memory credential, verify the same provider protocol used by a run, then run either workflow without editing JSON/YAML. Non-local/unknown execution is visibly warned because submitted text may leave the local domain.

OpenRouter defaults to `https://openrouter.ai/api/v1` and uses `OPENROUTER_API_KEY`; explicit reasoning levels are sent through its Chat Completions reasoning control. LM Studio defaults to `http://127.0.0.1:1234/v1`; an optional local-server token uses `LOCALMEDBOT_API_KEY`. LM Studio uses Responses for adjustable reasoning and native chat when reasoning is explicitly disabled. No model is silently selected and an explicit reasoning level is never silently downgraded.

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

The single workspace selector synchronises Clinical/Developer display with backend session access. Both workspaces have input/configuration, execution/progress and tabbed frozen run Input/Output panes. Developer mode exposes full workflow execution, run-wide output-repair and semantic-revision overrides, JSON/YAML inspection, self handoffs, development guideline selection/import, and a one-step tester with scratch/captured fixtures. See `docs/DEVEL.md`, `docs/self-execution.md`, `docs/fixtures.md`, `docs/versioning.md`, and `docs/upgrade-0.1.0.md`.

Developer-only commands (`profiles import-legacy`, `guidelines import`/`promote`, `steps`, `fixtures`, `self`) require the `--developer` flag.

## Clinical and security limits

This remains a single-user localhost evaluation prototype, not an authenticated clinical sign-off system. Source linkage and model checks do not establish clinical accuracy. SQLite/artifact storage is not encrypted. Provider warnings are advisory; the operator is responsible for not sending patient data outside the intended domain. Retrieval is bounded lexical search, not an exhaustive literature review.

The current product is not releaseable. The workflow and UI are intended for evaluation, not clinical deployment; clinical validation and release authorization remain separate requirements. See `NEWS.md` and `docs/versioning.md` for lifecycle status.
