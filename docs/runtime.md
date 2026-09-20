# Runtime semantics

## State and approval

Execution state is distinct from clinical acceptance. A model result can be syntactically valid and clinically unsupported. The runner validates module outputs, while required content checks and a human review decision control release.

Normal state sequence: pending → running → waiting_review → completed after approval. A failed gate produces blocked; a retryable transport failure exhausted within the invocation produces failed. Cancelled runs do not execute further. Blocked runs are preserved for investigation; start a new run after correcting configuration/input. Human revision of a reviewable result creates a pending continuation and invalidates its old approval.

The local reviewer name is attribution, not authentication. There is no automated claim of a real clinician having signed the demo output. Tests exercise the human-review interface using a fixture reviewer identity.

## Attempts, restart and invalidation

Each node attempt receives a number. Completed artifacts are immutable revisions with producing attempt, input revision references, schema and corpus revisions. New payload files are flushed before a transaction publishes their references and updates run state. An orphan file from a crash before commit is not an active artifact.

Successful model responses are persisted before node completion. A durable post-commit marker ensures an interrupted review transition is processed on restart before downstream work or completion. A restarted attempt reuses them by node/attempt/call index. If the process dies after an external call but before that response is stored, the call can repeat; the audit records the interrupted attempt. Exactly-once model execution is not claimed.

Review failure invalidates the target and descendants. Completed unaffected ancestors are reused. Old artifacts and dissent remain accessible by revision. A human request for revision does not reset budgets. Approval requires the current output revision and all mandatory checks. Stale review requests fail.

Snapshot contents include application manifest/version, runtime version, compiled workflow, prompts, schemas, templates, policy, input, model settings and corpus revision IDs. Sources and corpus revisions remain in the same data directory. Resume does not load updated application assets. Runtime-version mismatch requires an explicit migration decision rather than silent continuation. Trusted Python subclass code is not embedded in a run; retain the repository version that executed it.

One process owns mutations to a data directory. The browser serialises run execution and uses an in-process lock for mutations. Multiple independent writer processes or distributed workers are not supported.

## Budgets and model protocol

Turn, tool-call, token and active-execution-time budgets apply at run and node level. Transport retries, malformed-output repair and semantic revision all consume these budgets. Human waiting time is excluded. The HTTP timeout is bounded by the remaining execution time.

Token reservations use request byte count plus the configured maximum completion tokens; they are conservative estimates and are not refunded. Provider-reported usage is also retained when supplied. Neither reservation accounting nor provider reports constitute guaranteed billing totals. There is no price lookup or currency-cost promise.

Agent output is structured JSON with `action: search | read | submit`. Search/read arguments are validated and passed through an allow-listed gateway. No action can select additional stores, run shell commands, alter policy, approve artifacts or spawn another agent. The gateway supplies the run's frozen corpus scope.

Source content is treated as untrusted data in prompts and escaped text in the UI. These boundaries restrict tool permissions; they do not prove immunity to semantic prompt injection.

## Evidence decisions

Evidence discovered through authorised agent searches is collected into a validated, bounded candidate envelope before assessment. The matcher proposes support; a separate reviewer invocation audits the assigned support; disagreement triggers a separate adjudication node. The finaliser accepts only support decisions with eligible source IDs, and preserves all other findings as unresolved. No evidence is a legitimate result. An adjudicator cannot introduce an evidence ID outside the retrieved envelope.

The prototype uses one bounded evidence batch per answer, not an unbounded corpus traversal. The examples cap claims and retrieved candidates. Larger jobs should be partitioned by a deliberate application extension. Search remains lexical; evidence audit does not guarantee retrieval completeness.

Case facts use record provenance, while guideline claims use literature-style support assessments. Record statements do not establish treatment recommendations. The runtime does not interpret these clinical meanings.

## Data handling

API keys are referenced by environment-variable name and are attached only to outgoing HTTP headers. Logs exclude those headers and upstream error bodies. User-submitted record text and model responses are retained locally; de-identify input before use. A hosted model receives the declared prompt/input/source context.

The browser binds to localhost, checks host/origin and a request token on mutations, escapes source/model content and imposes an upload limit. This is not a production identity or security system. SQLite/artifact files are not encrypted and are accessible to the local operating-system user.
