# Isolated step fixtures

Developer mode can run one model-dependent workflow node without executing its ancestors or descendants. The input is the node's complete resolved input, not merely the previous node's output.

A step fixture records workflow/node identity, step-contract version, resolved inputs, complete evidence snapshots needed by the node, contextual review state, provenance, and data-origin metadata. Fixtures are provider-neutral.

- Local scratch fixtures are written below `.localmedbot/fixtures/scratch/` and are not repository assets.
- Repository fixtures live below `tests/fixtures/steps/<workflow>/<node>/`.
- Capturing a prior run uses the persisted resolved input from the selected attempt, so later upstream revisions cannot silently change it.
- Repository promotion requires a new immutable fixture identity/version, an explicit suitability declaration, an actor, and acknowledgement that the full fixture was reviewed for version control.
- Recorded response tapes are separate assets. A tape explicitly references one immutable fixture ID/version; multiple tapes may intentionally test alternative or failing model behaviours.

Do not place patient material in repository fixtures unless it has been deliberately reviewed and is authorised for repository storage.
