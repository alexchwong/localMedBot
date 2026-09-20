# localMedBot

A small local workbench for clinical workflow experiments: deterministic execution, bounded model reasoning, source-linked artifacts and explicit human review.

**Prototype only.** Bundled records and guidelines are fictional. Recorded demo responses validate software behaviour, not clinical accuracy. Live model acceptance is blocked in the build environment; see [verification and DoD](docs/verification.md).

## Start the working demo

Requires Python 3.10+ and a browser. Run from this repository's root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
localmedbot check
localmedbot serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). No model download or API key is needed for the **recorded** examples. Select a workflow, run it, inspect citations/checks, enter a reviewer name and approve the displayed revision. Requesting revision invalidates approval; use Resume to execute the revision.

The two applications are:

- **Clinical letter:** source-linked fact extraction, drafting, preservation/omission checks, bounded revision and approval. Examples cover a straightforward record, contradictory medication documentation and missing information.
- **Guideline QA:** typed corpus import, lexical retrieval, bounded search/read reasoning, matching, independent audit, conditional adjudication, rendering, content check and approval. Examples cover support, population mismatch, disagreement and no evidence.

Recorded mode is deliberately limited to the supplied input fixtures. It does not pretend to reason over arbitrary new input. Outputs, sources and events are persisted under `.localmedbot/`, excluded from git.

## Use a real model

Edit `config/local.yaml` with the model ID loaded on your local server and its OpenAI-compatible `/v1` base URL:

```bash
localmedbot --config config/local.yaml check
localmedbot --config config/local.yaml serve
```

For a hosted endpoint, edit `config/hosted.yaml`, set the environment variable named by `api_key_env` using your normal secret-management method, and select that file with `--config`. Never put a key into YAML. `check` validates configuration; it does **not** claim server connectivity or model quality.

The adapter uses `/chat/completions` with JSON response mode. Set `json_mode: false` if your server does not support that option. It still must return schema-valid JSON. Local and hosted endpoints share the same checks and approval gates. The model is explicitly configured; there is no implicit model auto-selection.

Optional role overrides let you choose different models or settings:

```yaml
roles:
  review:
    model: another-model-on-the-configured-endpoint
    temperature: 0
    max_tokens: 4096
```

Roles used by the examples are `extraction`, `reasoning`, `match` and `review`. A different reviewer model is optional; separate calls to the same model are not independent clinical assurance.

## Command-line use

```bash
localmedbot apps
localmedbot ingest guideline_qa
localmedbot run clinical_letter --example standard
localmedbot run guideline_qa --example conflict
localmedbot status RUN_ID
localmedbot resume RUN_ID
localmedbot review RUN_ID --revision 1 --actor "Reviewer" --decision approve
localmedbot review RUN_ID --revision 1 --actor "Reviewer" --decision revise --comments "Clarify chronology"
localmedbot cancel RUN_ID
```

Use the actual output revision returned by `status`; do not assume it remains 1 after revision. `run` and `resume` return a nonzero exit code when blocked or failed. Waiting for human review is a successful paused execution.

Custom input in HTTP mode:

```bash
localmedbot --config config/local.yaml run clinical_letter --input case.json
localmedbot --config config/local.yaml run guideline_qa --input question.json
```

Input files contain the `input` object from an example, not its response tape. Examples live in `applications/*/examples/`. The browser JSON editor accepts the same shape. A clinical record can be a JSON source, text or Markdown source inside `sources`. For narrative input without known fact IDs, set `required_fact_ids: []`; the independent preservation review must assess extraction completeness.

Global options `--apps`, `--data` and `--config` go before the command. CLI and browser use the same service. **Use only one writer process per data directory**; do not run CLI mutations concurrently with the browser server. Stop the server first. Single-process local operation is the prototype's concurrency contract.

## Corpus ingestion

Each application owns `ingestion.yaml`: formats, extraction mode, item schema, partition size, default indexes and typed index definitions. The guideline profile supports population, setting, publication date and priority; the record profile uses a different schema/index vocabulary.

```bash
localmedbot ingest guideline_qa --sources sources.json
```

`sources.json` is an array of source objects. See `applications/guideline_qa/corpus.json` for a full example. The browser also accepts `.md`/`.txt` uploads, wrapped as a text source. File uploads are capped; source IDs are identifiers, never file paths.

Direct JSON ingestion preserves each item with a JSON-pointer locator. Text/Markdown ingestion partitions immutable source text with character-range locators. To extract assertions using a model, set `mode: model` and an inline extraction `prompt` in the profile, then select an HTTP configuration. Model ingestion is budgeted and logged through the same runtime. JSON sources already contain structured items and do not need model extraction.

Imports publish atomically. Reimporting the same validated content/profile is idempotent. Changes create a new corpus revision; existing runs retain their earlier view. Case records are run-scoped, never added to the shared guideline store. Ingestion profiles can be changed without editing engine code.

Retrieval is SQLite-backed filtering with in-process lexical scoring over eligible items. This is appropriate for a small prototype corpus, not a large search service. Supported types: string, controlled string, boolean, number and ISO date; scalar or list. Query operators: equality, membership, and numeric/date ranges. Missing indexes are not negative clinical findings. Optional `overlay.include`/`overlay.exclude` item-ID lists in policy freeze the selection view into each run.

## Architecture

- `contracts.py`, `compiler.py`, `runtime.py`, `storage.py`: application-neutral execution and persistence.
- `modules.py`: subclassable ingestion, retrieval, reasoning, checks, evidence and rendering modules.
- `knowledge.py`: typed index validation and source-linked retrieval.
- `providers.py`: HTTP and explicitly recorded model adapters.
- `service.py`, `cli.py`, `web.py`: shared service and thin interfaces.
- `applications/`: YAML, schemas, prompts, templates, fictional sources and response fixtures.

Application modules use the `Module.execute(context, inputs, config)` contract. The runtime owns artifact publication, attempts, validation, budgets and approval. Evidence-chain stages are separate nodes, with inspectable results and conditional adjudication. YAML includes compose subworkflows; they do not import Python or evaluate expressions.

See [extension guide](docs/extending.md), [runtime semantics](docs/runtime.md), [tests](docs/testing.md), and [verification and DoD](docs/verification.md).

## Tests

```bash
python -m unittest discover -s tests -v
```

For real browser actions:

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

If Chromium is already installed, set `LOCALMEDBOT_BROWSER_EXECUTABLE` to its absolute executable path. Browser tests are opt-in so core CI needs no browser or model. The release verification includes a real Chromium run.

Tests check functions, schemas, state transitions, ID relationships and browser actions. There are no verbatim production-content assertions or output snapshots. Recorded model responses are fixture inputs, not expected output prose.

## Git-ready source

The ZIP contains source, application assets, tests, documentation and CI configuration. It excludes virtual environments, downloaded browsers, private run databases, credentials and build caches. Initialise your own repository after extracting:

```bash
git init -b main
git add .
git commit -m "Initial localMedBot prototype"
```

## Limits

This is a single-user localhost prototype, without authenticated clinical sign-off or EHR writeback. Content checks and evidence assessment remain fallible. It has no vector search, OCR, FHIR, plugin installer or autonomous delegation. Live model quality must be evaluated on your chosen endpoint before relying on results. Restart uses frozen assets and saved responses; an interrupted external call without a committed response may repeat. See [verification](docs/verification.md) for the exact acceptance exception.
