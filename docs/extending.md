# Extending localMedBot

## Configuration-only application

Copy an application directory, give its `application.yaml` a new ID/name/version, and edit its workflow, policy, prompts, schemas, rendering template and ingestion profile. Application IDs resolve through manifests; the engine has no clinical dispatch table.

A node declares an ID, registered module, dependencies, inputs, configuration and output JSON Schema. Schemas can be inline or paths. Prompt/template file contents are embedded at setup.

Clinical Letter's `application.yaml` declares a `purposes.yaml` asset with a default and unique options (`id`, `label`, `instructions`). The service exposes labels/IDs to the browser but freezes the selected complete entry into the run snapshot; `draft` and `draft_check` bind `genre` to `run.snapshot.selected_purpose`. Its render node uses `document: true, user_citations: false`; corpus-backed Guideline QA uses `user_citations: true`. The draft's `document` and `provenance` mappings are distinct: mapped passages must occur in the document and preserve fact evidence; the independent check must assess the full prose as well.

```yaml
nodes:
  - id: organise
    module: reason
    inputs:
      task: run.input
    config:
      prompt: organise.md
      role: reasoning
    schema: organised.schema.json
    repairs: 1
```

Bindings support `run.input`, `artifacts.NODE`, `artifacts.NODE.field` and `feedback.NODE`. Node IDs may contain dots; the longest matching node ID resolves first. A binding object can declare `optional: true`. Every artifact producer must be an ancestor. `needs` declares true data/control dependencies.

Conditions use `{from: artifacts.node.field, op: equals, value: true}`; `exists` and `in` are also supported. A skipped node supplies no artifact. A required missing binding blocks execution rather than fabricating a value.

A content-check node can declare:

```yaml
review:
  target: organise
  max_revisions: 1
```

On a failed check, the runner preserves its artifact, attaches feedback to the target, invalidates descendants and retries within that limit. Failed checks remain visible in history. Repair limits apply to syntax/schema/reference failures; review cycles are separate. All calls count against the same run-wide budget.

Policy `required_checks` names `content_check` nodes that must pass before approval. `human_approval: true` requires a final, unconditional `human_review` gate downstream of every node. Adapters cannot skip these checks. The workflow declares `output`, `review_node` and `revision_target` explicitly.

Use `includes: [component.yaml]` for reusable YAML subworkflows. Included nodes follow the same IDs, bindings and validation as inline nodes. `EvidenceChain.nodes(...)` is a helper for producing a reusable matching/audit/adjudication/finalisation component. It is expanded into ordinary persisted nodes, not an opaque agent.

## Application-defined ingestion and indexes

Example profile for a different clinical task:

```yaml
formats: [text, markdown, json]
mode: direct
chunk_chars: 1600
default_indexes:
  department: cardiology
indexes:
  department:
    type: string
    required: true
  encounter_dates:
    type: date
    many: true
  urgent:
    type: boolean
item_schema:
  type: object
  required: [id, text, indexes]
  properties:
    id: {type: string}
    text: {type: string}
    indexes: {type: object}
  additionalProperties: false
```

For model extraction use `mode: model`, add an inline `prompt`, and supply a real model configuration. Original source content is retained. Model-generated assertions are explicitly labelled `extracted_assertion` and point to their input chunk; an assertion is not presented as a verbatim excerpt.

Filters use application fields, e.g. `[{field: department, op: eq, value: cardiology}]`. `many: true` uses any matching value. Unknown fields, invalid types and unsupported operators fail explicitly. A `values` list defines a controlled string vocabulary. No clinical field names are reserved by the store.

## Subclass when behaviour changes

A subclass is appropriate for a new deterministic parser, a different retrieval strategy or a domain calculator. It is not needed to change prompts or schema fields.

```python
from localmedbot.modules import Renderer
from localmedbot.service import Service

class NamedRenderer(Renderer):
    def execute(self, context, inputs, config):
        result = super().execute(context, inputs, config)
        result.payload["document_kind"] = config["document_kind"]
        return result

service = Service(apps="applications", data="state", runs_root="runs")
service.registry.register("named_renderer", NamedRenderer())
```

Use `module: named_renderer` in that application's YAML and start the application through this trusted bootstrap. For a browser bootstrap, call `create_app(service).run(host="127.0.0.1", port=8765)`. No scheduler modifications are required. Duplicate registration is rejected. Plugins are trusted Python code, not a sandbox; models cannot register them.

Modules return `ModuleResult(payload)` and do not write artifact files or mark approvals. For model calls use `context.call(prompt, payload, schema, role)`; for agent tools use `context.tool(name, arguments)` so limits and audit apply. The `ReasoningHead` supplies a single-call mode and a bounded action-loop mode. `SyntaxCheck`, `SchemaCheck` and `ContentCheck` remain distinct classes.

Built-in agent tools are only `evidence.search` and `evidence.read`. Both require permission in the node and policy. A new external capability is a deliberate tool-gateway extension, not an arbitrary shell-command declaration in YAML. Side-effecting clinical tools are out of scope.

## Extension acceptance

Prove the extension with behaviour: run a new schema/index/prompt configuration, then exercise the specialised module through the same runner. Assert outputs satisfy the new schema and gates. Do not test the spelling of the prompt, template or source code. The included tests exercise both configuration changes and a shallow subclass.
