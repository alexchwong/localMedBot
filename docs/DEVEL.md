# Development procedure

`docs/devel-sysprompt.md` is the mandatory development-policy document. This file owns the executable repository procedure.

## Environment and canonical checks

Use the checkout-root `.env` for every repository Python command. The only unmanaged-Python exception is creating that environment.

POSIX:

```bash
python3 -m venv .env
source .env/bin/activate
.env/bin/python -m pip install -e '.[test]'
.env/bin/python scripts/maintenance.py check
.env/bin/python -m unittest discover -s tests -v
.env/bin/localmedbot check
```

Windows:

```powershell
py -3 -m venv .env
.env\Scripts\Activate.ps1
.env\Scripts\python.exe -m pip install -e ".[test]"
.env\Scripts\python.exe scripts\maintenance.py check
.env\Scripts\python.exe -m unittest discover -s tests -v
.env\Scripts\localmedbot.exe check
```

For browser verification see `docs/testing.md`. For fixtures and self execution see `docs/fixtures.md` and `docs/self-execution.md`.


## Maintained document ownership

- `README.md`: user/operator setup, commands, source-distribution layout and prototype limits.
- `docs/DEVEL.md`: repository maintenance and release procedures.
- `docs/devel-sysprompt.md`: mandatory human/agent development constraints.
- `docs/testing.md`: functional, browser and live verification distinctions.
- `docs/versioning.md`: product authority, release permission and independent persisted-contract versions.
- `docs/fixtures.md` and `docs/self-execution.md`: specialised developer contracts.
- `NEWS.md`: durable product-version change history and explicit release status.
- `docs/runtime.md`, `docs/extending.md` and `docs/upgrade-0.1.0.md`: maintained behavioural/extension/upgrade documentation.

## Product version change

For a new product version:

1. Change only the product authority in `src/localmedbot/version.json`, initially with `"releaseable": false`.
2. Update the bundled `applications/*/application.yaml` validated version copies.
3. Reinstall the editable package so distribution metadata reflects the new authority.
4. Add the durable change entry to `NEWS.md`.
5. Run maintenance, deterministic tests, preview build, exact verification and smoke verification.
6. If and only if the product is actually eligible, make a separate reviewed change setting `releaseable` to `true`; no tool grants this permission automatically.

Persisted schema/contract versions and guideline release IDs are independent; see `docs/versioning.md`.

## Preview package

From a clean committed checkout:

```bash
.env/bin/python scripts/release.py validate
.env/bin/python scripts/release.py build --preview --output dist/localMedBot-preview.zip
.env/bin/python scripts/release.py verify --archive dist/localMedBot-preview.zip
.env/bin/python scripts/release.py smoke --archive dist/localMedBot-preview.zip
```

Preview success does not authorize publication. `release.py release-check` is the read-only release-eligibility gate and fails while central permission is false. Publication automation is deliberately absent.

## Documentation and evidence retention

Maintain procedures in their owning documents instead of implementation/acceptance reports. Durable change history belongs in `NEWS.md`. Generated evidence, pass counts, transcripts and machine-generated test reports belong in ignored local output or ephemeral CI logs, not `docs/`. Active schemas, application examples, guideline snapshots/sources and reviewed `tests/fixtures/` assets remain repository inputs.

Manual review of changed tracked/staged content is still required; filename hygiene is not a general secret or patient-data scanner.
