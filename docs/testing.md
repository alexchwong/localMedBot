# Testing

Core deterministic verification:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Real-browser verification uses the existing Playwright extra when available:

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium
LOCALMEDBOT_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Tests assert state transitions, schemas, references, idempotency, scope, and other observable contracts rather than production prose. See `docs/development-instructions.md` for the mandatory verbatim-content rule.

Fake local HTTP tests validate protocol behaviour only. They do not establish live-model quality. OpenRouter and LM Studio acceptance must use real endpoints and versioned synthetic cases; a blocked endpoint remains an unmet acceptance item.
