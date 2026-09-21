# Versioning

localMedBot uses separate product and persisted-contract versions.

## Product authority and release permission

`src/localmedbot/version.json` is the sole product lifecycle authority. It contains exactly the canonical `MAJOR.MINOR.PATCH` product version and the boolean `releaseable` permission. Package metadata and `localmedbot.__version__` derive from that authority. Bundled `applications/*/application.yaml` version fields are validated copies, not independent authorities.

The product version is never restated as a test constant. Coherence across the authority, the bundled validated copies, package metadata, the runtime label and the CLI output is owned by the maintenance and release gates (`scripts/maintenance.py check`, `scripts/release.py`); a restated constant would be a second, unvalidated authority that must be edited by hand on every version change.

Release permission is fail-closed and separate from changing the product version. A new version starts with `releaseable: false`; setting it true requires a separate explicit reviewed change. Preview packages may be built while permission is false, but preview success is never release authorization.

## Independent persisted contracts

- Clinical-letter workflow asset version: **2**.
- Guideline-QA workflow asset version: **2**.
- Storage schema version: **1**.
- Run contract version: **1**.
- Model-step contract version: **1**.

Persisted contract numbers change only when their stored meaning becomes incompatible. Guideline sets have independent immutable release IDs (`v1`, `v2`, ...) and a mutable development generation. Promotion creates the next immutable release and advances only that set's default pointer; the previous default remains historical.

A product-version bump does not imply resume compatibility. The runtime already requires a stored run's `runtime_version` to equal the executing `localmedbot.__version__`. Consequently, unfinished 0.1.0 runs are not automatically resumable under 0.1.1: finish them with the matching software version or start a new run. Do not rewrite historical provenance or infer compatibility solely because the storage/run/step contract numbers are unchanged.
