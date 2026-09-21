"""Compile the bounded workflow vocabulary; freeze all referenced assets."""
from __future__ import annotations
from pathlib import Path
from copy import deepcopy
import json,yaml
from jsonschema import Draft202012Validator
from .contracts import Fault


def asset(root,name):
    p=(Path(root)/name).resolve(); base=Path(root).resolve()
    if not p.is_relative_to(base) or not p.is_file(): raise Fault("invalid_asset",str(name))
    return p

def artifact_ref(ref,node_ids):
    tail=ref.removeprefix("artifacts."); matches=[x for x in node_ids if tail==x or tail.startswith(x+".")]
    if not matches: raise Fault("missing_producer",ref)
    node=max(matches,key=len); return node,tail[len(node):].lstrip(".")

def load_application(root,registry):
    root=Path(root); manifest=yaml.safe_load(asset(root,"application.yaml").read_text(encoding="utf-8"))
    workflow=yaml.safe_load(asset(root,manifest["workflow"]).read_text(encoding="utf-8")); policy=yaml.safe_load(asset(root,manifest["policy"]).read_text(encoding="utf-8"))
    for include in workflow.pop("includes",[]):
        component=yaml.safe_load(asset(root,include).read_text(encoding="utf-8")); workflow["nodes"].extend(component["nodes"])
    for node in workflow["nodes"]:
        cfg=node.setdefault("config",{})
        for key in ["prompt","template"]:
            if key in cfg and isinstance(cfg[key],str): cfg[key]=asset(root,cfg[key]).read_text(encoding="utf-8")
        if isinstance(cfg.get("profile"),str): cfg["profile"]=yaml.safe_load(asset(root,cfg["profile"]).read_text(encoding="utf-8"))
        if isinstance(cfg.get("conflict_schema"),str): cfg["conflict_schema"]=json.loads(asset(root,cfg["conflict_schema"]).read_text(encoding="utf-8"))
        for key in ["schema","input_schema"]:
            if isinstance(node.get(key),str): node[key]=json.loads(asset(root,node[key]).read_text(encoding="utf-8"))
        node["model_dependent"]=bool(node.get("model_dependent",registry.get(node["module"]).model_dependent))
    result={"manifest":manifest,"workflow":workflow,"policy":policy}
    return compile_workflow(result,registry)

def compile_workflow(snapshot,registry):
    wf,policy=snapshot["workflow"],snapshot["policy"]; nodes=wf.get("nodes",[]); ids=[n["id"] for n in nodes]
    if not nodes or len(ids)!=len(set(ids)): raise Fault("duplicate_or_empty_nodes")
    by_id={n["id"]:n for n in nodes}; done=set(); ordered=[]; ancestors={}
    while len(done)<len(nodes):
        ready=[n for n in nodes if n["id"] not in done and set(n.get("needs",[]))<=done]
        if not ready: raise Fault("invalid_dependency_graph")
        for n in ready: done.add(n["id"]); ordered.append(n)
    for n in ordered:
        name=n["id"]; ancestors[name]=set(n.get("needs",[]))
        for dep in n.get("needs",[]): ancestors[name]|=ancestors[dep]
        registry.get(n["module"]); Draft202012Validator.check_schema(n.get("schema",{})); Draft202012Validator.check_schema(n.get("input_schema",{}))
        for binding in n.get("inputs",{}).values():
            ref=binding.get("from") if isinstance(binding,dict) else binding
            if not isinstance(ref,str): raise Fault("invalid_binding",name)
            if ref.startswith("artifacts."):
                if artifact_ref(ref,by_id)[0] not in ancestors[name]: raise Fault("missing_producer",ref)
            elif not (ref=="run.input" or ref.startswith("run.") or ref.startswith("feedback.")): raise Fault("invalid_binding",ref)
        cond=n.get("when")
        if cond:
            if cond.get("op") not in {"exists","equals","in"}: raise Fault("invalid_condition")
            ref=cond.get("from","")
            if not ref.startswith("artifacts.") or artifact_ref(ref,by_id)[0] not in ancestors[name]: raise Fault("invalid_condition_binding")
        review=n.get("review")
        if review and (review.get("target") not in ancestors[name] or not isinstance(review.get("max_revisions"),int) or review["max_revisions"]<0): raise Fault("invalid_review")
        if n.get("repairs",0)<0: raise Fault("invalid_retry_limit")
    for required in policy.get("required_checks",[]):
        if required not in by_id or by_id[required]["module"]!="content_check": raise Fault("missing_required_check",required)
    output=wf.get("output")
    if output not in by_id: raise Fault("missing_output")
    if policy.get("human_approval"):
        gate=wf.get("review_node")
        if gate not in by_id or by_id[gate]["module"]!="human_review": raise Fault("missing_human_gate")
        if by_id[gate].get("when") or set(ids)-{gate}!=ancestors[gate] or output not in ancestors[gate]: raise Fault("invalid_human_gate")
    for check in policy.get("required_checks",[]):
        if check not in ancestors.get(wf.get("review_node"),set()): raise Fault("ungated_check",check)
    limits=policy.get("limits",{})
    defaults={"turns":120,"tool_calls":40,"tokens":4_000_000,"seconds":1800,"node_turns":24,"node_tool_calls":20,"node_tokens":1_500_000,"node_seconds":600}
    for k,v in defaults.items(): limits.setdefault(k,v)
    for k,v in limits.items():
        if k in defaults and (isinstance(v,bool) or not isinstance(v,(int,float)) or v<=0): raise Fault("invalid_budget",k)
    policy["limits"]=limits; wf["nodes"]=ordered
    return snapshot
