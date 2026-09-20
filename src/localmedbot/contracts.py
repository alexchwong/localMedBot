"""Small public contracts; payload schemas belong to applications."""
from dataclasses import dataclass, field
from typing import Any
import json
from jsonschema import Draft202012Validator


class Fault(Exception):
    def __init__(self, code, detail="", findings=None):
        self.code, self.detail, self.findings = code, detail, findings or []
        super().__init__(f"{code}: {detail}")


def validate(value, schema):
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise Fault("schema_invalid", findings=[
            {"code": "schema_invalid", "path": list(e.absolute_path), "rule": e.validator}
            for e in errors])
    return value


def parse(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        raise Fault("syntax_invalid") from e


@dataclass(frozen=True)
class Artifact:
    id: str
    revision: int
    payload: Any
    producer: str
    inputs: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    id: str
    revision: str
    kind: str
    content: Any
    scope: str


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    source: str
    locator: str
    text: str
    indexes: dict
    revision: str
    assertion_kind: str = "excerpt"


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    evidence_ids: list[str]
    kind: str = "assertion"
    qualifiers: dict = field(default_factory=dict)
    revision: int = 1


@dataclass(frozen=True)
class SupportAssessment:
    claim_id: str
    evidence_ids: list[str]
    decision: str
    reason: str


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
    revision: int
    actor: str
    decision: str
    comments: str


class Module:
    """Trusted extension point. The runner owns commit, retry and approval."""
    def execute(self, context, inputs, config) -> ModuleResult:
        raise NotImplementedError


class Registry:
    def __init__(self):
        self.modules = {}

    def register(self, name, module):
        if name in self.modules:
            raise Fault("duplicate_module", name)
        if not isinstance(module, Module):
            raise Fault("invalid_module", name)
        self.modules[name] = module

    def get(self, name):
        if name not in self.modules:
            raise Fault("unknown_module", name)
        return self.modules[name]
