# Versioning

localMedBot uses separate product and persisted-contract versions.

- Product/package version: **0.1.0** (`localmedbot.__version__` and `pyproject.toml`).
- Clinical-letter workflow asset version: **2**.
- Guideline-QA workflow asset version: **2**.
- Storage schema version: **1**.
- Run contract version: **1**.
- Model-step contract version: **1**.

The technical specification's document revision `v2` is not a software version. Persisted contract numbers are incremented only when their stored meaning becomes incompatible. Unversioned pre-0.1.0-v2 runs are treated as legacy read-only records after additive migration; missing provenance is never fabricated.

Guideline sets have independent immutable release IDs (`v1`, `v2`, ...) and a mutable development generation. Promotion creates the next immutable release and advances only that set's default pointer; the previous default remains a historical release.
