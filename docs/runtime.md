# Runtime semantics

localMedBot uses one centralized runtime layout. Relative defaults are resolved from the launch directory: `state/` contains the shared SQLite database, preferences, registry metadata and writer lock; `runs/<run-id>/` contains one run's immutable inputs, frozen configuration, attempts, model request/response records, artifacts, logs and derived `output.md`; `config/` contains tracked execution defaults; reviewed fixtures remain under `tests/fixtures/` and scratch fixtures under ignored `tests/fixtures/scratch/`. `applications/`, `model_profiles/` and `guideline_sets/` remain their canonical authored locations.

`--data` is retained as an alias for the shared state root. `--runs-root` and `--config-root` independently override their roots. Normal startup does not create a new empty installation beside an unmigrated default `.localmedbot`; use `localmedbot relocate --source ...` offline. Relocation is journalled in the destination state root and is restartable, validates copied bytes/database mappings before activation, does not overwrite divergent destinations, and leaves unknown legacy files in place.

## Execution and inspection

Runs freeze compiled workflow assets, effective role/model settings, clinical input, selected guideline release/corpus, source provenance, retry limits and contract versions. New generated run IDs contain a UTC timestamp, workflow ID and random suffix and never contain patient identifiers.

Run states distinguish queued/pending, executing, retrying, waiting for a `self` response, waiting for human review, blocked, failed, cancelled and completed presentation states. A blocked/failed stage is persisted with the same terminal state rather than being left labelled as executing. Human or `self` waiting time is not charged to model active-time limits.

Every model-dependent stage attempt owns one logical model operation. Each initial/continuation/output-repair request has a stable request ID; each HTTP dispatch attempt or self response opportunity has a stable physical-call ID. Transport retries stay under the same request, output repair stays in the same operation/stage attempt, and checker-directed semantic revision creates a new target attempt. Replays and duplicate self submissions do not create new usage or retry consumption.

Before an external dispatch or self handoff, request files and pending reservations are persisted. Immutable files are staged, flushed, atomically renamed to unique final paths, then referenced from SQLite in one transaction that also advances the run's monotonically increasing inspection revision. Inspection reads indexed state in one SQLite snapshot and exposes only committed files. Startup removes identified temporary files, marks unreferenced immutable files as uncommitted, rebuilds derived views, and blocks affected execution if a committed file reference is missing.

The browser polls active, self-waiting and review-waiting runs about once per second. Responses carry `run_id` and `inspection_revision`; stale responses from an earlier selection/revision are ignored while client-owned form edits and inspector selection remain local. Poll failures are shown explicitly.

## Retry and usage accounting

`config/execution.json` defines the central defaults for output-repair retries and semantic-revision retries. The shipped default is three retries after the initial candidate for each class. Effective limits are resolved once at run creation with precedence: developer run-wide override, then authored node/check override, then workflow policy override, then central default. Blank developer fields inherit and zero disables the corresponding retry class.

Output repair uses one workflow-neutral deterministic audit: parse first, then all applicable JSON Schema findings, then registered reference/contract/protocol validation. The failed raw response, complete current findings and exact corrective feedback are persisted and reused in the actual repair request. Invalid model-authored tool/protocol requests are rejected before execution. Provider/setup failures and tool runtime failures are not output repair.

One aggregation implementation supplies both persisted `logs/model-usage.json` and the UI. It distinguishes logical operations, model requests, actual HTTP calls, recorded replay calls, self handoffs, output repairs, semantic revisions and transport retries. Provider-reported token, duration and cost data are retained when supplied; unavailable values remain unavailable rather than becoming measured zero, and partially reported totals are marked partial.

OpenRouter uses OpenAI-compatible `/chat/completions`. LM Studio uses `/v1/responses` for `default`, `low`, `medium`, and `high` reasoning, and native `/api/v1/chat` with reasoning disabled for `none`. If `/v1/responses` is unavailable, LM Studio may fall back to `/v1/chat/completions` only when reasoning is `default`; an explicit reasoning level is never silently discarded. localMedBot does not require provider-side JSON mode: model text is audited against the unchanged local schema and repaired through the bounded runtime when necessary. Redirects are rejected. Credentials come from the in-memory browser vault or the profile's environment variable and are excluded from persisted state. A provider call left in an uncertain dispatch state requires explicit Resume before a repeat dispatch.

Continuation requires the stored run's product `runtime_version` to match the executing product version. A product bump does not automatically make an unfinished run resumable even when persisted-contract numbers are unchanged; see `docs/versioning.md`.
