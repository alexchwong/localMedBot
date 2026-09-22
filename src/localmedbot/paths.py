"""Centralized filesystem layout for runtime state, runs, config, and fixtures."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .contracts import Fault


@dataclass(frozen=True)
class RuntimePaths:
    launch_root: Path
    state_root: Path
    runs_root: Path
    config_root: Path
    fixture_root: Path
    scratch_root: Path
    legacy_default_root: Path

    @classmethod
    def resolve(cls, *, launch_root=None, state_root=None, runs_root=None, config_root=None, fixtures_root=None):
        launch=Path(launch_root or Path.cwd()).resolve()
        state=Path(state_root).resolve() if state_root is not None else (launch/"state").resolve()
        runs=Path(runs_root).resolve() if runs_root is not None else (launch/"runs").resolve()
        config=Path(config_root).resolve() if config_root is not None else (launch/"config").resolve()
        fixtures=Path(fixtures_root).resolve() if fixtures_root is not None else (launch/"tests"/"fixtures").resolve()
        return cls(launch,state,runs,config,fixtures,fixtures/"scratch",launch/".localmedbot")

    def ensure_runtime_dirs(self, *, writer: bool, explicit_state: bool=False):
        if not explicit_state and self.legacy_default_root.exists() and not self.state_root.exists():
            raise Fault("legacy_data_relocation_required", str(self.legacy_default_root))
        journal=self.state_root/"relocation-journal.json"
        if journal.exists():
            try:
                import json
                doc=json.loads(journal.read_text(encoding="utf-8"))
            except Exception:
                raise Fault("relocation_journal_invalid",str(journal)) from None
            if not doc.get("completed"):
                raise Fault("relocation_incomplete",str(journal))
        if writer:
            self.state_root.mkdir(parents=True,exist_ok=True)
            self.runs_root.mkdir(parents=True,exist_ok=True)
            self.scratch_root.mkdir(parents=True,exist_ok=True)
        return self

    def run_root(self, run_id: str) -> Path:
        return self.runs_root/run_id


def default_execution_config(config_root: Path) -> dict:
    import json
    path=Path(config_root)/"execution.json"
    try:
        doc=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError,TypeError) as exc:
        raise Fault("execution_config_invalid",str(path)) from exc
    if not isinstance(doc,dict) or doc.get("schema_version")!=1:
        raise Fault("execution_config_invalid",str(path))
    allowed={"schema_version","output_repair_retries","semantic_revision_retries"}
    if set(doc)!=allowed:
        raise Fault("execution_config_invalid",str(path))
    for key in ("output_repair_retries","semantic_revision_retries"):
        value=doc.get(key)
        if isinstance(value,bool) or not isinstance(value,int) or value<0:
            raise Fault("execution_config_invalid",key)
    return doc
