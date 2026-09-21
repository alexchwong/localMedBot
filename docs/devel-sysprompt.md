# localMedBot development instructions

These instructions are agent-neutral and apply to human developers, Cline, ChatGPT Work, Claude Code, and future coding agents working in this repository. Follow `docs/DEVEL.md` for the repository execution procedure; all repository Python execution is through the checkout's `.env` unless the procedure explicitly identifies the bootstrap exception.

## Testing rule

> No test may assert verbatim material as an invariant unless the asserted material is exclusively test-controlled: it comes from a version-controlled test fixture, or the test itself authors it. Never assert verbatim material that repository code produces or that is read from a non-test repository asset, including when the test copies it into a local variable first.

- A test fixture is a version-controlled asset that exists only to serve tests, lives below `tests/fixtures/`, and is explicitly version-specified: step fixtures and recorded tapes by immutable id and version, live cases by versioned filename. A file being called a fixture is not enough. Application examples, prompts, schemas, guideline snapshots and sources, HTML, source constants and other production assets remain production assets even when a test reads them.
- Every test whose assertions depend on a test fixture must declare that dependency with a fixture tag naming the repository-relative fixture path, so the dependency is visible in the test and in runner output. A tag must name an existing tracked path below `tests/fixtures/`. An undeclared fixture dependency and a tag with no corresponding use are both defects. The declaration helper and the tag-enforcement module are `tests/support.py` and `tests/test_fixture_policy.py`; `docs/testing.md` owns the convention.
- Never restate a repository artifact label as a test constant. Product version, distribution metadata, archive and tag names, and manifest entries are labels of the repository artifact, not invariants of behaviour: coherence is owned by the version-representation and release gates, and a restated label is an unvalidated copy of a single authority. See `docs/versioning.md`.

Stable error codes, IDs, states, schema relationships, source relationships, and explicitly versioned fixture bytes are valid test contracts. Production prompt wording, guideline prose, HTML pages, and generated clinical prose are not golden outputs. A file being called a fixture is not enough if it is merely a copied production asset.

## Runtime and clinical boundaries

- Use the existing bounded workflow engine; do not add a parallel clinical execution path for HTTP, recorded, `self`, or isolated-step testing.
- Model output is untrusted until syntax, schema, source-reference, and required semantic checks pass.
- Keep evidence access within the frozen run/fixture corpus scope. Never let model or source text grant new tools, files, URLs, commands, or approval authority.
- Keep execution finite: calls, tools, repair, active time, and revisions remain bounded.
- Preserve every distinct clinical fact by default. Human omission is explicit, revision-specific, reasoned, and acknowledged at final approval.
- Present incompatible guideline recommendations as sourced alternatives. Do not silently rank a winner by model preference, source date, priority metadata, or adjudication.
- Human review is explicit and revision-specific. Mandatory failed checks are never overridable.
- Never persist provider credentials, authorization headers, or known credential values in runs, events, model traces, fixtures, exports, or reports.
- `self` means the coding/frontier-model session supplies only model response content. The local runtime executes tools, validates outputs, and changes workflow state.

## Completion rule

Implement and verify every applicable Definition of Done item in the implementation specification governing the change. A missing external credential, unreachable model endpoint, unavailable browser runtime, package-index failure, or externally enforced network restriction may block applicable evidence; it does not permit substituting another path and claiming the blocked criterion passed. Continue all independent work and report genuinely blocked criteria precisely.
