# localMedBot — Implementation Baseline

Status: adopted baseline. Repository name selected: **localMedBot**. Completion evidence and the live-model exception are recorded in `verification.md`.

## 1. Goal and agreed scope

Build a new, small Python repository for clinical workflows using deterministic orchestration with bounded model reasoning. NGS Evidence Layer (NEL) remains separate and serves as architectural inspiration. Backwards compatibility and NGS migration are not prototype requirements.

The prototype must provide:

- Task-configurable corpus ingestion and application-defined typed indexes.
- Generic module classes with shallow subclassing for genuinely different behaviour.
- A reasoning head, evidence chain, and distinct syntax, schema and content checks.
- Two functioning applications: clinical-letter drafting and guideline question answering.
- A small, explicitly fictional guideline corpus and synthetic clinical records.
- Configurable local and hosted model endpoints, plus recorded responses for offline verification.
- A thin local browser interface with source inspection, review, revision and approval.
- Persistent artifacts, bounded retries, tool permissions, audit events and restart/resume.

Keep one Python process, SQLite, local artifact files, YAML workflow definitions and JSON Schema. Start with sequential scheduling of ready nodes. Avoid distributed infrastructure and framework dependencies that duplicate the runner.

## 2. Repository name

The selected repository name is **localMedBot**; the Python package and CLI use `localmedbot`.

## 3. Architecture and implementation boundaries

### 3.1 Three layers

| Layer | Responsibility | Must not contain |
| --- | --- | --- |
| Runtime | Compilation, scheduling, attempts, artifact revisions, permissions, budgets, persistence, review gates | Clinical stage names, disease rules, application-specific fields |
| Reusable modules | Ingestion, retrieval, reasoning, checks, evidence chain, rendering, human review | Hidden application selection or independent persistence/retry systems |
| Applications | Schemas, prompts, workflows, ingestion profiles, indexes, policies, templates and specialised tools | Copies of the execution engine |

Use configuration first. Subclass only when an algorithm or capability differs. Keep subclass depth to one application layer where practical. Dependencies such as the model client and evidence store are injected services, not inherited global state.

The runtime enforces lifecycle and permissions around every module call. A subclass cannot bypass approval by marking its own output approved.

### 3.2 Suggested repository layout

| Location | Contents |
| --- | --- |
| `src/clinical_workflow/runtime/` | Contracts, compiler, runner, state, registry, policy and tool gateway |
| `src/clinical_workflow/modules/` | Generic module classes and reusable compositions |
| `src/clinical_workflow/providers/` | Model adapter and recorded-response adapter |
| `src/clinical_workflow/store/` | SQLite persistence, artifact access and retrieval |
| `src/clinical_workflow/web/` | Thin server, templates and minimal browser assets |
| `applications/clinical_letter/` | First application package and synthetic record fixtures |
| `applications/guideline_qa/` | Second application package and fictional guideline sources |
| `tests/` | Functional contract, integration and interface tests |
| `docs/` | Setup, extension guide and prototype limitations |

Include `pyproject.toml`, a small configuration example and a root README. Exclude run data, credentials and local databases from git. No plugin marketplace, frontend build pipeline or separate service is required.

## 4. Shared contracts

Implement small typed contracts, without a universal clinical ontology.

| Contract | Minimum contents |
| --- | --- |
| `Artifact` | ID, revision, schema reference, payload reference, producing attempt, input revision references |
| `Source` | ID, revision, source kind, original content reference, import metadata, application/run scope |
| `EvidenceItem` | ID, revision, source/locator references, excerpt or extracted assertion, typed indexes, extraction provenance |
| `Claim` | ID, revision, proposition, application-defined kind, source-fact references, structured qualifiers when needed |
| `SupportAssessment` | Claim revision, evidence revisions, support/contradiction/insufficiency decision, applicability and rationale |
| `CheckResult` | Check type, pass/fail/needs-review status, findings with codes, severity and artifact/field references |
| `ModuleResult` | Outputs and execution outcome; review decisions remain separate |
| `ReviewDecision` | Exact artifact revision, actor, decision, comments and timestamp |

Clinical facts can initially be application-schema artifacts with source references. Preserve missing, explicitly absent and uncertain findings as distinct values.

Separate execution success, semantic acceptance and human approval. A model response passing JSON Schema is not automatically evidence-supported or approved.

Extracted assertions must identify themselves as interpretations of source material. Preserve original passages so reviewers can inspect them. A documented patient fact and a literature recommendation use the same provenance infrastructure but different relationship meanings.

## 5. Generic module classes

All modules implement a common execution contract with typed inputs, configuration and outputs. The runner supplies a constrained context: artifact access, authorised services, cancellation/deadline information and remaining budgets.

| Class | Required behaviour | Application specialisation |
| --- | --- | --- |
| `Ingestor` | Read sources, partition content, extract items, validate, persist provenance | Extraction prompts, partition settings, schemas; subclass only for new parsing behaviour |
| `Retriever` | Filter and rank eligible items; return source-linked results | Query construction or ranking strategy |
| `ReasoningHead` | Produce structured results from declared inputs; optionally iterate over allowed tools | Task prompts, output schema, permitted tools and stopping criteria |
| `EvidenceChain` | Compose matching, audit and conditional adjudication into accepted support and unresolved findings | Claim applicability criteria and evidence policy |
| `SyntaxCheck` | Parse structured output and identify malformed content | Supported format; JSON first |
| `SchemaCheck` | Validate parsed objects and reference integrity | Application JSON Schemas and registered validators |
| `ContentCheck` | Assess preservation, omission, contradiction or support | Deterministic rules and separate model reviewer prompts |
| `Renderer` | Render structured results and resolve citations | Templates and output schema |
| `HumanReview` | Pause and request a decision on a specific revision | Review form and permitted actions |

`EvidenceChain` should be a reusable subworkflow whose component attempts remain visible and resumable. Do not hide several model calls in an opaque operation.

## 6. Workflow language and policy

Use YAML describing registered module IDs, explicit dependencies, input bindings, output schemas and bounded control structures. Module registration occurs in trusted application code; YAML does not import arbitrary modules or evaluate Python.

Support only the controls needed by the examples:

- Explicit dependencies and artifact input references.
- Simple declared conditions using existence, equality and membership.
- Reusable subworkflows.
- Bounded review/revision loops with explicit target, feedback binding and exhaustion action.
- Bounded item batches for evidence work; no general-purpose map/reduce language is required.
- Persistent human-review pauses.

Compilation checks unknown modules, unavailable inputs, cycles outside supported review loops, duplicate outputs, missing schemas/prompts and policy incompatibilities. A required input from a skipped node must fail or use an explicitly declared alternative; skipping is not successful data production.

Keep workflow and policy separate files within each application, resolved into one run snapshot. Policy defines required checks, evidence requirements by claim kind, allowed tools, revision limits and approval requirements. Runtime configuration supplies endpoint credentials indirectly through environment variables.

Both prototype applications require human approval. Required checks cannot be bypassed by changing a model adapter or leaving a node out. If a required review cannot run, stop or remain blocked.

## 7. Execution, persistence and budgets

- Store runs, attempts, events, review decisions and evidence metadata in SQLite. Store larger immutable payloads in local artifact files.
- Publish an artifact reference only after its file is durably written; use a database transaction for state transitions. An interrupted write must not appear completed.
- Each new revision retains its predecessor. Downstream results identify exact input revisions.
- Revision invalidates affected descendants and their approvals; unaffected completed branches can be reused.
- Snapshot workflow, policy, prompts, schemas, model settings, application version and source/evidence revisions at run creation.
- Resume completed attempts from persisted results. Never silently reinterpret an existing run using changed application assets.
- Read-only model/tool calls interrupted before committing a result may be repeated; record this limitation rather than promising exactly-once external execution.
- Use explicit states including pending, running, waiting for review, blocked, failed, cancelled and completed. Approval is separately recorded.
- Distinguish transport retry, syntax/schema repair and semantic revision. Define retry counts as additional attempts after the first.
- Enforce per-node and run-wide turn, tool-call, token and time budgets. Repairs and revisions consume the same global budget.
- Reserve a configured maximum before issuing a model call. Record provider-reported usage when available and estimates otherwise. Do not imply exact billing guarantees.
- Log permitted tool calls, their inputs/results, model requests/results, checks, invalidations, approvals and termination reasons. Exclude credentials. Do not require private model reasoning traces.

## 8. Corpus ingestion and retrieval

### 8.1 Ingestion profiles

Each application supplies a profile declaring source formats, partition strategy, extraction mode, prompt/schema, typed index definitions and validation policy.

Support Markdown/text and structured JSON initially. Use heading/paragraph or bounded-length partitioning. Record stable section/paragraph or JSON-pointer locators against the saved source revision. Support direct extraction for already structured fixtures and model extraction for narrative sources.

Index declarations support string, controlled string, boolean, number and date values, with explicit scalar/list cardinality. Implement equality, membership and range filters as appropriate. Defer hierarchical vocabularies until a real application needs them. Unknown index fields or invalid values are rejected explicitly.

Import into a draft batch, validate, then publish a corpus revision. Failed imports must not partially replace the active corpus. Reimporting identical source/profile inputs must not create duplicate active evidence. Changed inputs produce a new revision while earlier runs retain their pinned view.

The guideline and record profiles must use different schemas/indexes. Records remain scoped to their case/run; they are not added to a shared literature corpus.

### 8.2 Retrieval

Use SQLite typed filtering and lexical ranking. A query includes store/scope, search text, filters and result limit. Results include evidence/source revisions, locators and ranking information.

Expose supported query capabilities. Unsupported operations fail clearly; semantic search is not simulated by silently dropping its request. Empty results are valid and must reach the reasoning head as insufficient evidence.

Deterministic nodes and agents use the same retrieval API and permission checks. The runtime injects case/store access scope; a model cannot widen it through query parameters.

### 8.3 Overlay scope

For the prototype, support simple include/exclude selection rules as a frozen retrieval profile. Defer a CUL-style editor and assertion amendment system. Local guidance can be ingested as a separately attributed source. Never overwrite original source text to represent local opinion.

## 9. Reasoning and evidence behaviour

The model adapter supports configurable endpoint, model, role, sampling settings, response limits and timeouts. Start with one compatible HTTP protocol for local/hosted endpoints; do not promise universal provider compatibility.

`ReasoningHead` has two modes:

1. Single structured model task, used by letter drafting and extraction.
2. Bounded action loop, used by guideline QA, allowing search, read and submit-result actions.

A validated structured action protocol is sufficient initially; native provider tool calling is optional. Reject unknown actions, disallowed tools, invalid arguments and out-of-scope source IDs. Tool responses are data, not authority to change policy. The model does not spawn agents, edit the corpus or approve output.

The evidence chain operates on immutable claim/evidence revisions:

1. Retrieve eligible candidates or use the reasoning head's declared candidates.
2. Match support and identify contradiction or insufficient applicability.
3. Audit selected support in a separate invocation/context.
4. Perform a bounded reassignment or conditional adjudication when required.
5. Emit accepted support, rejected pairs and unresolved findings without erasing dissent.

No evidence is a valid unresolved outcome. Adjudication cannot invent missing support. Retrieval completeness is not guaranteed by auditing selected matches. Limit claims and candidates per batch so local models receive bounded contexts.

Final answer rendering uses accepted claim IDs and validated source references. If prose drafting introduces new assertions or changes qualifiers, the preservation/content check must fail or request revision. Runtime checks enforce known IDs and review status; semantic accuracy remains a model/human judgment.

## 10. Two working applications

### 10.1 Clinical letter

Workflow: import synthetic record → extract source-linked facts → select facts for communication purpose → draft structured letter → preservation/omission checks → bounded revision → render → human approval.

Use an application schema for recipient role, purpose, problems, medication facts and follow-up. No sending or real recipient resolution is required. Literature evidence is optional and absent from this example.

Provide at least three synthetic cases: a straightforward case, a case with conflicting or temporally distinct statements, and a case with missing information. Mandatory facts are declared in fixture data so omissions can be tested structurally.

### 10.2 Guideline QA

Workflow: ingest fictional guideline sources → accept question/context → bounded reasoning/search/read → structured claims → evidence chain → render source-linked answer → content check → human approval.

Provide approximately 8–12 evidence items across at least three short fictional sources. Include population/setting distinctions, a date or version distinction, a contradictory pair and an intentionally unsupported topic. Mark every source and resulting demo output as synthetic; do not present fictional recommendations as genuine clinical guidance.

Demonstrate a supported answer, an applicability mismatch, unresolved contradiction and no-evidence response. The corpus must be ingested through the real ingestion path, not embedded as a special-case answer table.

## 11. Thin browser interface and CLI

Both interfaces call the same runtime service. The browser supports application selection, profile-based corpus import, input submission, run status, source inspection, check findings, revision requests and approval/rejection. Polling is sufficient; no streaming infrastructure is required.

Approval binds to the displayed output revision. Stale approval requests are rejected. Run restart/resume works after a server restart. Display failed/blocked states distinctly from completed drafts.

Use escaped text rendering for uploaded content and model output. Restrict upload types/size and prevent path traversal. Bind locally by default. A self-declared local reviewer name is adequate for the prototype but is not authenticated clinical sign-off.

Provide CLI operations for configuration validation, ingestion, run, status, resume and review, plus application discovery. The CLI is also the simplest end-to-end verification boundary.

## 12. Implementation sequence, tests and stage completion

Each stage must leave a runnable increment. Integrate the letter application before extending the architecture for guideline QA. Do not build unused abstraction layers in anticipation of future applications.

| Stage | Implementation tasks | Functional verification | Stage done when |
| --- | --- | --- | --- |
| 1. Contracts and scaffold | Package, contracts, registry, application discovery, config validation | Valid/invalid contract objects; duplicate registration; discover two application manifests | Clean install and application/config inspection work |
| 2. Runtime | Compiler, bindings, persistence, attempts, budgets, revision and review state | Dependencies, skips, invalid graph, retry exhaustion, failure injection, resume and stale approvals | A deterministic fixture workflow executes and resumes without losing committed results |
| 3. Model/check modules | HTTP adapter, recorded adapter, reasoning head, syntax/schema/content interfaces | Controlled HTTP responses, malformed outputs, repair bounds, timeout, denied tool and budget termination | Model task and checks work through the runner with inspectable attempts |
| 4. Ingestion/store | Source revisions, profiles, extraction, index validation, publication, lexical retrieval | Two different profiles, duplicate import, failed-batch rollback, filters/ranges, empty search, case isolation | Both source types ingest and retrieve without application fields in the engine |
| 5. Letter slice | Application prompts/schema/workflow, checks, renderer and human gate | Source-linked facts, omission/conflict cases, revision, final approval invalidation | Letter workflow works offline and with a live configured model |
| 6. Guideline slice | Bounded search/read reasoning, evidence chain, fictional corpus and citations | Supported/mismatched/conflicting/missing evidence; invented IDs; conditional adjudication | Guideline workflow works using the existing engine and explicit extension points |
| 7. Browser and CLI | Shared service endpoints, minimal pages, import, review and resume | Browser actions, escaped content, stale review, source opening, CLI exit/status behaviour | Both workflows can be operated from input through approval in the browser |
| 8. Release verification | Focused CI, setup guide, extension guide and limitations | Clean installation, complete functional suite, live acceptance runs and application extension exercise | Global definition of done below is satisfied |

If stage 6 needs a missing generic capability, implement and test that capability explicitly. Do not conceal clinical branching in the runner. After the shared vocabulary is complete, verify a config-only workflow variation requires no core edits.

## 13. Test policy: function only

This is a binding implementation requirement.

**Tests assert behaviour, structured relationships, state transitions and externally meaningful outcomes. They must not guard verbatim production content. Verbatim-content assertions are allowed only for test fixture inputs.**

Do not use:

- Golden snapshots of generated letters, answers, prompts, YAML files, README text or rendered pages.
- Assertions that production prompts or source files contain particular sentences or code fragments.
- Exact error-message matching, exact HTML/text rendering comparisons or output file hashes as regression expectations.
- Fixed prompt lengths, documentation wording, line counts or internal call order unrelated to a contract.
- Exact model reasoning text, exact prose, citation numbering snapshots or expected punctuation.

Do use:

- Structured status enums, error codes, schema validity, types, counts and ID/reference relationships.
- Assertions that required facts/claims are represented by IDs, unsupported claim IDs are excluded, and source locators resolve.
- Budget ceilings, number of allowed retries, required review transitions and blocked release behaviour.
- Parsed output structure and working source/citation links rather than exact Markdown or HTML.
- Browser actions verified by state changes and accessible controls rather than text snapshots.
- Parametrised cases and transformations that preserve meaning: reordering independent inputs, changing harmless formatting, adding irrelevant evidence and changing index field names.

Protocol enum equality is a functional assertion, not a prose snapshot. Source/fixture integrity may be checked against fixture input bytes where needed; this exception must not be used to freeze generated output as a new golden fixture.

Recorded model/tool responses are **inputs** that exercise the system. The expected result is a functional property, not a byte-for-byte copy of the resulting artifact. For example, feed a recorded unsupported claim and assert that its ID cannot enter an approved answer.

Use a lightweight standard test runner, preferably Python `unittest`, plus a small browser test dependency only for the UI acceptance path. Do not require paid model calls in CI. Do not set arbitrary coverage percentages or add tests that mirror trivial implementation details.

### 13.1 Required functional test matrix

| Area | Positive case | Negative or boundary case | Assertion basis |
| --- | --- | --- | --- |
| Workflow compiler | Resolve dependencies and bindings | Cycle, unknown module, missing producer, required input skipped | Compile result/error code and reachable operations |
| Schema checks | Accept valid typed object | Missing field, wrong type, unknown reference | Structured findings and acceptance state |
| Syntax repair | Malformed input corrected within limit | Repeated malformed output exhausts limit | Attempt counts and final state |
| Semantic revision | Reviewer feedback produces accepted revision | Review repeatedly fails | Revision ancestry, invalidation and blocked approval |
| Persistence | Resume after completed attempt | Interrupt between payload write and state commit | No partial completion and consistent recovered state |
| Ingestion | Two application-specific profiles | Invalid indexes, duplicate input, failed extraction | Item counts, typed fields, active corpus revision |
| Retrieval | Filter/rank eligible fixture evidence | Empty result, unsupported operator, cross-case query | Membership, limits and isolation |
| Reasoning loop | Search/read/submit within limits | Unknown action, denied tool, endless search | Tool dispatch, termination reason and budget ceilings |
| Evidence chain | Accepted claim-support pair | Invented IDs, mismatch, contradiction, no evidence | Pair eligibility, review status and unresolved findings |
| Rendering | Render accepted structured content | Unsupported or stale claim reference | Claim membership and resolvable citations |
| Human review | Approve displayed revision | Approve old revision after edit | Decision revision and release eligibility |
| Interface | Submit, inspect, revise and approve | Unsafe upload/path, unescaped source content | HTTP/browser effects and persisted state |
| Extensibility | Run changed prompt/schema/index configuration | Incompatible output binding | Working result or explicit validation failure |

### 13.2 Live model acceptance

Run both workflows with a real configured endpoint. Exercise at least one local endpoint and one hosted endpoint across the acceptance set to substantiate both adapter configurations. A recorded adapter must never be reported as live model validation. If access is unavailable, report that acceptance gate as pending.

For each run, retain input/source revisions, endpoint/model identity excluding secrets, check results, usage, output revision and human review observations. Assess preservation, applicability and supported conclusions using a short rubric; accept valid paraphrases. Independent model review is useful evidence but not an infallible test oracle.

Live runs need not be repeated after unrelated documentation or cosmetic changes. Re-run only the affected workflow when prompts, model integration, evidence semantics or execution contracts change.

## 14. Definition of done

The rapid prototype is complete only when all applicable items below are demonstrated.

### Product

- Fresh installation follows the README and starts one local browser application.
- Both workflows are selectable and operate from input to human-approved output.
- Guideline sources are visibly fictional and pass through real configurable ingestion.
- Source passages, evidence decisions and review findings are inspectable.
- Missing evidence, contradictions and failed checks produce explicit outcomes rather than fabricated support.

### Architecture

- The runtime contains no clinical-letter, guideline, gene, disease or NGS-specific dispatch.
- Generic modules have a documented execution contract and can be specialised through configuration or shallow subclasses.
- New application index names/types are validated and queried without engine modification.
- A config-only variation of an example workflow runs without core edits; an example specialised module registers without changing scheduler code.
- Required checks and approval are enforced independently of provider adapter choice.

### Execution and provenance

- Every final output identifies its input, source, evidence, configuration and review revisions.
- Syntax/schema repair and semantic revision are separately bounded and globally budgeted.
- Agent tools are allow-listed and scope-restricted; no model-controlled bypass exists in the supported interface.
- Stop/restart/resume preserves committed results and reports any repeated interrupted external call.
- Revising an artifact invalidates dependent results and prior approval as appropriate.
- A failed check cannot silently become an approved clinical output.
- Credentials are absent from repository fixtures and persisted request logs.

### Verification and delivery

- Functional tests cover the matrix above and pass from a clean checkout.
- Tests contain no verbatim production-content guards or output golden snapshots.
- Both examples pass recorded-response end-to-end verification and live model acceptance.
- Browser verification covers submission, source inspection, review, revision and stale-approval rejection.
- README includes installation, endpoint configuration, ingestion, example runs, resume and review.
- Extension guide shows configuration-only changes and the boundary requiring a subclass/tool.
- Known limitations and pending gates are explicit; no unverified capability is labelled complete.

## 15. Deferred scope and risks

Defer NGS migration, FHIR/EHR connectors, PDF/OCR ingestion, vector search, hierarchical ontologies, arbitrary workflow expressions, visual workflow editing, distributed workers, autonomous delegation, persistent agent memory, external plugin installation, a full overlay editor and clinical-system writeback.

Do not make emulation of OpenWorker or another general-purpose agent platform a dependency. Adopt a specific external pattern only when it simplifies a required capability. No verified project-specific OpenWorker assessment is assumed by this plan.

Principal limitations:

- Synthetic examples demonstrate software function, not clinical safety or medical correctness.
- Content review can miss errors; model agreement is not independent clinical validation.
- Lexical retrieval can miss relevant material; a support audit does not establish corpus completeness.
- Local models vary in schema/action reliability; endpoint compatibility and observed limitations must be documented.
- Recorded replay reproduces execution decisions; live reruns may produce different reasoning and prose.
- The local single-user UI has no enterprise identity, access management or authenticated clinical sign-off.
- Real patient deployment requires separate privacy, security and clinical validation work. This prototype uses synthetic or de-identified inputs.

## 16. Architectural references

These NEL references informed the earlier architectural review; they are inspiration, not runtime dependencies. Links track the moving master branch.

- [Declarative workflow](https://github.com/alexchwong/ngs_evidence_layer/blob/master/workflows/proforma_v1/workflow/default.yaml)
- [Workflow runner and review feedback](https://github.com/alexchwong/ngs_evidence_layer/blob/master/workflows/proforma_v1/engine/workflow_runner.py)
- [Evidence assignment, audit and adjudication mechanics](https://github.com/alexchwong/ngs_evidence_layer/blob/master/workflows/proforma_v1/engine/evidence.py)
- [Corpus user layer](https://github.com/alexchwong/ngs_evidence_layer/blob/master/docs/cul.md)
- [Recorded-response replay](https://github.com/alexchwong/ngs_evidence_layer/blob/master/workflows/proforma_v1/replay.py)
