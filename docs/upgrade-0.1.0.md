# 0.1.0 upgrade notes

This 0.1.0 implementation replaces the earlier global provider configuration with workflow-specific model profiles. Existing unversioned runs remain inspectable but are read-only under the new run contract. The first writer opening a legacy database creates a SQLite backup before additive migration.

Legacy `config/local.yaml` / `config/hosted.yaml` style files can be imported into the matching workflow profile:

```bash
localmedbot --developer profiles import-legacy \
  --workflow clinical_letter --file config/hosted.yaml
```

Only unambiguous OpenRouter or local-network LM Studio configurations are imported. Secrets remain environment/memory-only and are not copied from YAML.

Workflow asset version is now 2 for both applications. Storage, run, and step contracts are version 1. Product version remains 0.1.0; the specification document's revision `v2` is not a product-version suffix.
