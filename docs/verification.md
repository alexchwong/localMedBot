# Verification and definition of done

Release: localMedBot 0.1.0. Scope: the agreed rapid prototype, using synthetic/de-identified inputs and two example applications.

## Verification result

**47 tests passed, with zero failures, errors or skips**, from a clean extracted checkout and a fresh virtual environment on 20 September 2026. The installed non-editable package, CLI, application assets and real Chromium browser were exercised. Details are recorded in `test-results.json`. The implementation includes functional contract tests, local HTTP protocol tests and real Chromium browser actions. Recorded response tapes are fixture inputs; no generated prose, production prompt or HTML snapshot is asserted verbatim.

**One acceptance exception remains: live model inference.** There is no configured real model endpoint/model identity or credential in the build environment, and connection probes to the standard local inference ports were refused. `live-environment-check.json` records the checks without secret values. The user's own local model server is not exposed to this workspace. A mock HTTP server is not counted as model inference.

The repository supports real HTTP model calls and includes endpoint configurations and a live acceptance runner. The inability to perform live inference here is an environment/access exception permitted by the request, not a passed test or a claim of clinical quality.

## Definition-of-done mapping

| # | Requirement | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Fresh installation and local browser start | PASS | Clean virtual environment installation; installed CLI check; real browser server launch |
| 2 | Both workflows operate through approval | PASS | All seven fixture examples; browser letter and guideline review actions |
| 3 | Fictional guidelines use real ingestion | PASS | Nine items from three synthetic sources imported through application profile |
| 4 | Sources, evidence decisions and findings inspectable | PASS | Source API/browser action; artifact and execution panels; preserved dissent |
| 5 | Missing evidence, contradictions and check failures explicit | PASS | Mismatch, conflict and no-evidence examples; blocking review/omission tests |
| 6 | Runtime has no clinical application dispatch | PASS | Runtime/compiler/store source review; application manifests drive discovery |
| 7 | Generic contract and shallow specialisation | PASS | Public Module interface; extension guide; registered renderer subclass test |
| 8 | Application-defined indexes without engine changes | PASS | Distinct record/guideline profiles; department/list/boolean/date query tests |
| 9 | Configuration and subclass extensions need no scheduler edits | PASS | Changed template/prompt/schema configuration and registered subclass tests |
| 10 | Checks and approval independent of provider | PASS | Same runner for recorded and HTTP fixtures; mandatory unconditional gate validation |
| 11 | Output lineage includes input/source/configuration/review revisions | PASS | Artifact metadata, immutable corpus references, frozen run snapshot, revision-bound decisions |
| 12 | Repairs and semantic revision bounded and globally budgeted | PASS | Repair exhaustion, semantic cycles, turn/tool/token/time budget tests |
| 13 | Agent tools allow-listed and scope-restricted | PASS | Denied tool, invalid arguments, cross-scope access and invented evidence tests |
| 14 | Restart preserves committed results and records interrupted calls | PASS | Crashes before commit, after review commit and after human-gate commit; response replay |
| 15 | Revision invalidates descendants and approval | PASS | Automatic and human revision tests; unchanged ancestors reused |
| 16 | Failed check cannot become an approved output | PASS | Required checks, omission failures, exhausted review and stale approval rejection |
| 17 | Credentials absent from fixtures and logs | PASS | Environment-only auth; header-only fixture sentinel; inspect/log exclusion test |
| 18 | Functional suite passes from a clean checkout | PASS | `test-results.json`; installed package exercised with repository application assets |
| 19 | No verbatim production-content guards | PASS | Test source review: only protocol values, relationships and fixture-input integrity assertions |
| 20 | Recorded-response and live-model end-to-end acceptance | PARTIAL — EXCEPTION | Recorded flows pass. Real inference blocked by unavailable model access; see exception above |
| 21 | Browser submission, sources, review, revision and stale approval | PASS | Real Chromium tests, including safe text rendering and mobile width check |
| 22 | README covers setup/configuration/ingestion/run/resume/review | PASS | Root README, runnable CLI commands and config files |
| 23 | Extension guide covers configuration and subclasses | PASS | `extending.md`, example subclass and exercised registry interface |
| 24 | Limitations and pending gates explicit | PASS | This report, README, runtime/testing guides and environment evidence |

“Approval” in verification means exercising the application's human-review interface with a fixture reviewer identity. It does not represent clinical sign-off by a clinician. Source-code review and documentation inspection are manual verification, not brittle content tests.

## Complete the live acceptance gate on a configured machine

Set up a real local endpoint and a hosted endpoint, edit the model IDs in the two config files, and provide the hosted key through its configured environment variable. Then run:

```bash
python scripts/live_acceptance.py --local-config config/local.yaml --hosted-config config/hosted.yaml
```

This executes the letter through the local model and guideline QA through the hosted model. Both must reach the review gate. Inspect each saved run using the CLI or browser pointed at `.localmedbot/live-acceptance`; review factual preservation, population applicability, unsupported claims and citations. Approve only the exact reviewed revision and record model identities, run IDs, outcomes and observations here. Valid paraphrases are accepted; exact prose comparison is inappropriate.

Do not mark the live gate passed solely because a provider returned HTTP 200 or a JSON object passed its schema.

## Prototype limits retained deliberately

- Single local writer process per data directory; no multi-user production service.
- Lexical retrieval and bounded evidence batches; no vector index, OCR, FHIR or EHR writeback.
- Model content checks can miss errors; selected-pair audit does not prove retrieval completeness.
- No authenticated reviewer identity, encryption-at-rest or clinical validation.
- Hosted model use sends declared context to that provider.
- A source checkout with application assets is the supported distribution; the package alone does not bundle clinical applications into site-packages.
