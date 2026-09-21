# Self execution

`self` is a development executor that lets the current frontier/coding-model session act as the model for each logical model call while localMedBot retains control of workflow state, validation, tools, evidence scope, and review gates.

1. Start a developer run with the workflow-specific self profile.
2. Inspect/export the pending handoff. It contains the exact messages, output schema, action protocol, evidence scope, and request identity.
3. The frontier model reasons only over that handoff and writes the raw model response text into a response envelope.
4. Submit the response. localMedBot validates it and either continues, runs a bounded tool requested by the model, exposes a repair handoff, or reaches the next node.
5. Repeat until `waiting_review` or another terminal state.

CLI example:

```bash
.env/bin/localmedbot --developer run clinical_letter \
  --profile clinical_letter.self.default \
  --input synthetic-letter-input.json --input-mode free_text
.env/bin/localmedbot --developer self export RUN_ID --output handoff.json
.env/bin/localmedbot --developer self submit RUN_ID --response response.json
```

The response file is:

```json
{"contract_version":1,"request_id":"...","content":"raw model response text"}
```

The model must not directly edit SQLite/artifacts or execute evidence tools in its own shell. Search/read actions are returned as model content and localMedBot executes them within the frozen evidence scope.

## Isolated model-step testing with `self`

The one-step tester can use `self` exactly where an OpenRouter or LM Studio profile would normally execute a model call. The selected node is materialized with its resolved fixture input and normal prompt/schema/tool contract, but without full-workflow ancestor/downstream transitions.

For example:

```bash
.env/bin/localmedbot --developer steps run clinical_letter draft \
  --fixture tests/fixtures/steps/clinical_letter/draft/fixture-clinical_letter.draft.standard.v1.json \
  --profile clinical_letter.self.default

.env/bin/localmedbot --developer self export RUN_ID --output handoff.json
# The current frontier/coding-model session writes response.json for this handoff.
.env/bin/localmedbot --developer self submit RUN_ID --response response.json
```

The same pattern applies to every model-dependent node returned by `.env/bin/localmedbot --developer steps list WORKFLOW`, including model-backed content checks and the guideline match/audit/adjudication stages. For agentic reasoning, a self response may request `search` or `read`; localMedBot executes that bounded tool itself and returns the observation in the next handoff.
