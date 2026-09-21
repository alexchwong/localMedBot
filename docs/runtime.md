# Runtime semantics

localMedBot persists immutable node artifacts plus complete resolved step attempts and model/tool journals. Every logical model call is written before external execution and is validated by the same parser/schema path for OpenRouter, LM Studio, recorded, self, and the isolated-step tester.

Run states are `pending`, `running`, `waiting_model`, `waiting_review`, `completed`, `failed`, `blocked`, `rejected`, and `cancelled`. `waiting_model` is an intentional self handoff. `rejected` is a human disposition and preserves the rejected artifact; only explicit revision reopens it. Mandatory semantic exhaustion may be human-revisable, while integrity/budget/configuration/internal blocks are not.

OpenRouter and LM Studio use OpenAI-compatible `/chat/completions`. Redirects are rejected. Credentials come from the in-memory browser vault or the profile's environment variable and are excluded from persisted state. A provider call left in `dispatching` by a crash becomes `external_response_unknown`; only explicit Resume authorises a resend and records that double billing may be possible.

Runs freeze compiled workflow assets, effective role/model settings, clinical input, selected guideline release/corpus, origins, and contract versions. Later profile/guideline/prompt changes do not alter a paused or historical run.

Continuation also requires the stored run's product `runtime_version` to match the currently executing product version. A product bump therefore does not automatically make an unfinished run resumable even when persisted-contract numbers are unchanged; see `docs/versioning.md`.
