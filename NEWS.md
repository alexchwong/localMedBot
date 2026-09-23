# Change history

## 0.1.4 — unreleased; not releaseable

- Observe background worker completion, including a worker that returns while its run is still active, and reconcile abandoned pending or running runs at writer startup. Interrupted executions become durable failures that continue only through explicit resume; an uncertain provider dispatch additionally requires acknowledgement before a request can be repeated.
- Project frozen workflow stages, persisted attempts, model calls, physical calls, output repairs and semantic revisions into read-only chronological inspection. The progress rail counts model stages reached, not completed or approved; reached and valid stage indicators remain distinct, including after revisions. Show frozen retry limits and distinguish feedback recorded from feedback actually dispatched.
- Add optional operator-supplied run titles, newest-first run summaries, workflow filtering and independently refreshed browser history. Inspecting an older run does not replace the tracked live run; the browser offers a return-to-live control. Missing committed artifacts remain unavailable and explicitly degrade inspection rather than appearing as valid output. Full execution inputs and responses in the new projection remain Developer-only.
- Replace the clinical middle-pane disclosure lists with tabs for model calls, repairs and omission editing. Select recorded calls by stage and call/retry, browse history by workflow, local date and title, and request draft revision explicitly for reasoned omissions. The RHS input view displays submitted text with its original line breaks and the selected purpose.

Release permission remains false; preview verification does not grant publication or clinical validation.

## 0.1.3 — unreleased; not releaseable

- Unify Clinical and Developer workspace selection with backend session mode, and separate workflow input, execution inspection and tabbed frozen run input/output into three browser panes.
- Configure Clinical Letter purposes in workflow assets; freeze the selected genre instructions and draft the actual document with internal passage/fact provenance and independent checking. Internal clinical-note references no longer render as citations; Guideline QA retains external citations.
- Provide readable structured error and verification details with Developer JSON/YAML presentation, and launch the quiet local browser server automatically unless `--no-browser` is specified.

Release permission remains false; preview verification does not grant publication or clinical validation.

## 0.1.2 — unreleased; not releaseable

- Rework the browser into primary Clinical and Developer workspaces with compact model settings, readable live run state, final revision-specific review, deliberate omission editing and an ordinary run/attempt/call inspector.
- Add durable logical-operation/request/physical-call accounting, provider usage aggregation, live self/provider status and monotonic inspection revisions with stale-poll protection.
- Add one workflow-neutral deterministic audit and bounded output-repair facility, plus persisted checker-directed semantic-revision feedback. Defaults allow three retries after the initial candidate for each retry class and can be overridden run-wide in Developer mode.
- Replace active `.localmedbot` storage with centralized `state/`, `runs/`, `config/` and fixture-scratch locations; add staged immutable file publication, startup reconciliation and explicit journalled legacy relocation.
- Keep `src/localmedbot/version.json` as the only authored current product version and inject it into bundled application snapshots/UI/API/package metadata.
- Add the versioned clinical-letter `self` acceptance input and v0.1.2 regression coverage for retry identities/accounting, storage recovery, relocation and polling/inspection contracts.

Release permission remains false. Completion of 0.1.2 engineering verification is not publication authorization or clinical validation.

## 0.1.1 — unreleased; not releaseable

- Centralise product version and explicit release permission in `src/localmedbot/version.json`, with package/runtime derivation.
- Standardise repository Python execution on `.env` and add maintained development/release procedures.
- Remove historical implementation/acceptance evidence from maintained documentation.
- Add tracked/staged hygiene checks, a curated Git-snapshot release manifest, deterministic source ZIP construction, exact archive verification and isolated extracted-package smoke verification.
- Add a fail-closed read-only release eligibility guard. Publication automation remains deliberately absent.
