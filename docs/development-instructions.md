# localMedBot development instructions

These instructions are agent-neutral and apply to human developers, Cline, ChatGPT Work, Claude Code, and future coding agents working in this repository.

## Testing rule

> Do not create tests that assert verbatim content unless the input to the function being tested is a version-controlled or explicitly version-specified test fixture. Do not test verbatim content from actual repository code or from non-test repository assets.

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

Implement and verify every applicable Definition of Done item in `tech_specs_0.1.0_v2.md` when that specification is supplied for a 0.1.0 implementation. A missing external credential, unreachable LM Studio endpoint, unavailable browser runtime, or externally enforced network restriction may block acceptance evidence; it does not permit substituting recorded inference and calling the live criterion passed. Continue all independent work and report blocked criteria precisely.
