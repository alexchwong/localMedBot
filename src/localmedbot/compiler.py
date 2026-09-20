"""Compile a deliberately bounded workflow vocabulary, with no eval/imports."""
from copy import deepcopy
from pathlib import Path
import json
import yaml
from .contracts import Fault
from jsonschema import Draft202012Validator


def asset(root, name):
    p = (Path(root) / name).resolve()
    if not p.is_relative_to(Path(root).resolve()) or not p.is_file():
        raise Fault("invalid_asset", str(name))
    return p


def artifact_ref(ref, node_ids):
    tail = ref.removeprefix("artifacts.")
    matches = [x for x in node_ids if tail == x or tail.startswith(x + ".")]
    if not matches:
        raise Fault("missing_producer", ref)
    node = max(matches, key=len)
    return node, tail[len(node):].lstrip(".")


def load_application(root, registry):
    root = Path(root)
    manifest = yaml.safe_load(asset(root, "application.yaml").read_text())
    workflow = yaml.safe_load(asset(root, manifest["workflow"]).read_text())
    policy = yaml.safe_load(asset(root, manifest["policy"]).read_text())
    for include in workflow.pop("includes", []):
        component = yaml.safe_load(asset(root, include).read_text())
        workflow["nodes"].extend(component["nodes"])
    # All referenced assets are embedded, so resume never rereads mutable files.
    for node in workflow["nodes"]:
        cfg = node.setdefault("config", {})
        for key in ["prompt", "template"]:
            if key in cfg:
                cfg[key] = asset(root, cfg[key]).read_text()
        if isinstance(node.get("schema"), str):
            node["schema"] = json.loads(asset(root, node["schema"]).read_text())
    result = {"manifest": manifest, "workflow": workflow, "policy": policy}
    compile_workflow(result, registry)
    return result


def compile_workflow(snapshot, registry):
    wf, policy = snapshot["workflow"], snapshot["policy"]
    nodes = wf.get("nodes", [])
    ids = [n["id"] for n in nodes]
    if not nodes or len(ids) != len(set(ids)):
        raise Fault("duplicate_or_empty_nodes")
    by_id = {n["id"]: n for n in nodes}
    done, ordered = set(), []
    while len(done) < len(nodes):
        ready = [n for n in nodes if n["id"] not in done and set(n.get("needs", [])) <= done]
        if not ready:
            raise Fault("invalid_dependency_graph")
        for n in ready:
            done.add(n["id"]); ordered.append(n)
    ancestors = {}
    for n in ordered:
        name = n["id"]
        ancestors[name] = set(n.get("needs", []))
        for dep in n.get("needs", []):
            ancestors[name] |= ancestors[dep]
        registry.get(n["module"])
        Draft202012Validator.check_schema(n.get("schema", {}))
        for binding in n.get("inputs", {}).values():
            ref = binding.get("from") if isinstance(binding, dict) else binding
            if not isinstance(ref, str):
                raise Fault("invalid_binding", name)
            if ref.startswith("artifacts."):
                if artifact_ref(ref, by_id)[0] not in ancestors[name]:
                    raise Fault("missing_producer", ref)
            elif not (ref == "run.input" or ref.startswith("feedback.")):
                raise Fault("invalid_binding", ref)
        cond = n.get("when")
        if cond:
            if cond.get("op") not in ["exists", "equals", "in"]:
                raise Fault("invalid_condition")
            ref = cond.get("from", "")
            if not ref.startswith("artifacts.") or artifact_ref(ref, by_id)[0] not in ancestors[name]:
                raise Fault("invalid_condition_binding")
        review = n.get("review")
        if review:
            if review.get("target") not in ancestors[name] or not isinstance(review.get("max_revisions"), int) or review["max_revisions"] < 0:
                raise Fault("invalid_review")
        if n.get("repairs", 0) < 0:
            raise Fault("invalid_retry_limit")
    for required in policy.get("required_checks", []):
        if required not in by_id or by_id[required]["module"] != "content_check":
            raise Fault("missing_required_check", required)
    output = wf.get("output")
    if output not in by_id:
        raise Fault("missing_output")
    if policy.get("human_approval"):
        if wf.get("review_node") not in by_id or by_id[wf["review_node"]]["module"] != "human_review":
            raise Fault("missing_human_gate")
        if by_id[wf["review_node"]].get("when") or set(ids)-{wf["review_node"]} != ancestors[wf["review_node"]]:
            raise Fault("invalid_human_gate")
        if output not in ancestors[wf["review_node"]]:
            raise Fault("invalid_human_gate")
    for check in policy.get("required_checks", []):
        if check not in ancestors.get(wf.get("review_node"), set()):
            raise Fault("ungated_check", check)
    limits = policy.get("limits", {})
    for field in ["turns", "tool_calls", "tokens", "seconds", "node_turns", "node_tool_calls", "node_tokens", "node_seconds"]:
        if not isinstance(limits.get(field), (int, float)) or limits[field] <= 0:
            raise Fault("invalid_budget", field)
    wf["nodes"] = ordered
    return snapshot
