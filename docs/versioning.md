# Versioning

localMedBot uses separate product and persisted-contract versions.

## Product authority and release permission

`src/localmedbot/version.json` is the sole authored current product lifecycle authority. It contains exactly the canonical `MAJOR.MINOR.PATCH` product version and the boolean `releaseable` permission. Package metadata and `localmedbot.__version__` derive from that authority. HTML/API presentation and bundled application snapshots receive the resolved product version at load/compile time; bundled `applications/*/application.yaml` files deliberately do not duplicate it.

The product version is never restated as a production/test authority. Maintenance and release gates verify derivation and reject a manually authored bundled application product-version literal. Historical NEWS/upgrade prose, stored provenance, independent schema versions and synthetic test-authored versions are not competing current-product authorities.

Release permission is fail-closed and separate from changing the product version. A new version starts with `releaseable: false`; setting it true requires a separate explicit reviewed change. Preview packages may be built while permission is false, but preview success is never release authorization.

## Independent persisted contracts

- Clinical-letter workflow asset version: **2**.
- Guideline-QA workflow asset version: **2**.
- Storage schema version: **1**.
- Run contract version: **1**.
- Model-step contract version: **1**.

Persisted contract numbers change only when their stored meaning becomes incompatible. Guideline sets have independent immutable release IDs (`v1`, `v2`, ...) and a mutable development generation.

A product-version bump does not imply resume compatibility. The runtime requires a stored run's `runtime_version` to equal the executing `localmedbot.__version__`. Historical runs keep their recorded product version/provenance and remain inspectable; incompatible unfinished runs require the matching software version or a new run.
