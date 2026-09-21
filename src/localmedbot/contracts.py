"""Public contracts and stable faults for localMedBot 0.1.0."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import json
import unicodedata
from jsonschema import Draft202012Validator


class Fault(Exception):
    def __init__(self, code: str, detail: str = "", findings: list[dict] | None = None):
        self.code, self.detail, self.findings = code, detail, findings or []
        super().__init__(f"{code}: {detail}")


class ModelSuspension(Exception):
    """Intentional self-executor suspension; never treated as a workflow fault."""
    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(request_id)


def validate(value: Any, schema: dict) -> Any:
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise Fault("schema_invalid", findings=[
            {"code": "schema_invalid", "path": list(e.absolute_path), "rule": e.validator}
            for e in errors
        ])
    return value


def parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise Fault("syntax_invalid") from exc


def validate_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise Fault("actor_invalid")
    actor = actor.strip()
    if not 1 <= len(actor) <= 100 or any(unicodedata.category(c) == "Cc" for c in actor):
        raise Fault("actor_invalid")
    return actor


@dataclass(frozen=True)
class Artifact:
    id: str
    revision: int
    payload: Any
    producer: str
    inputs: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    status: str
    findings: list[dict] = field(default_factory=list)


@dataclass
class ModuleResult:
    payload: Any
    outcome: str = "complete"


@dataclass(frozen=True)
class ReviewDecision:
    revision: int | None
    actor: str
    decision: str
    comments: str


class Module:
    model_dependent = False
    def execute(self, context, inputs, config) -> ModuleResult:
        raise NotImplementedError


class Registry:
    def __init__(self):
        self.modules: dict[str, Module] = {}

    def register(self, name: str, module: Module) -> None:
        if name in self.modules:
            raise Fault("duplicate_module", name)
        if not isinstance(module, Module):
            raise Fault("invalid_module", name)
        self.modules[name] = module

    def get(self, name: str) -> Module:
        if name not in self.modules:
            raise Fault("unknown_module", name)
        return self.modules[name]
