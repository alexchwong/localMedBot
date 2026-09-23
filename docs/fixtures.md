# Isolated step fixtures

Developer mode can run one model-dependent workflow node without executing its ancestors or descendants. The input is the node's complete resolved input, not merely the previous node's output.

A step fixture records workflow/node identity, step-contract version, resolved inputs, complete evidence snapshots needed by the node, contextual review state, provenance, and data-origin metadata. Fixtures are provider-neutral.

Clinical Letter workflow asset v3 uses configured purpose IDs and a drafted document with passage/fact provenance. New draft-step fixtures and tapes must carry the resolved `genre` input and use the v3 document output schema. Existing v1 recorded assets remain immutable historical fixtures; captured node configuration alone is not a historical node output schema.

- Local scratch fixtures are written below ignored `tests/fixtures/scratch/` (or the configured fixture root's `scratch/`) and are not reviewed repository assets.
- Repository fixtures live below `tests/fixtures/steps/<workflow>/<node>/`.
- Capturing a prior run uses the persisted resolved input from the selected attempt, so later upstream revisions cannot silently change it.
- Repository promotion requires a new immutable fixture identity/version, an explicit suitability declaration, an actor, and acknowledgement that the full fixture was reviewed for version control.
- Recorded response tapes are separate assets. A tape explicitly references one immutable fixture ID/version; multiple tapes may intentionally test alternative or failing model behaviours.

Do not place patient material in repository fixtures unless it has been deliberately reviewed and is authorised for repository storage.

A test whose assertions depend on a repository fixture declares that dependency with a fixture dependency tag; see `docs/testing.md` and the policy in `docs/devel-sysprompt.md`.
