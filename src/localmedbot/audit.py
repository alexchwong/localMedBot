"""Workflow-neutral deterministic audit and model-output repair feedback."""
from __future__ import annotations
from copy import deepcopy
import json
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from .contracts import Fault


def _pointer(parts):
    if not parts: return ""
    return "/"+"/".join(str(p).replace("~","~0").replace("/","~1") for p in parts)


def _stable(value):
    try: return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    except Exception: return repr(value)


def _schema_leaf_errors(error, branch=None):
    if not error.context:
        yield error,branch
        return
    if error.validator in {"oneOf","anyOf","allOf"}:
        for idx,child in enumerate(error.context):
            child_branch=f"{error.validator} alternative {idx+1}"
            yield from _schema_leaf_errors(child,child_branch)
    else:
        yield error,branch
        for child in error.context:
            yield from _schema_leaf_errors(child,branch)


def _schema_findings(value,schema):
    try: Draft202012Validator.check_schema(schema)
    except SchemaError as exc: raise Fault("schema_definition_invalid",str(exc)) from None
    findings=[]
    validator=Draft202012Validator(schema)
    roots=list(validator.iter_errors(value))
    expanded=[]
    for err in roots:
        leaves=list(_schema_leaf_errors(err))
        expanded.extend(leaves or [(err,None)])
    for err,branch in expanded:
        code="schema."+str(err.validator or "invalid")
        expected=deepcopy(err.validator_value)
        received=deepcopy(err.instance)
        problem=err.message
        correction=f"Make {_pointer(err.absolute_path) or '/'} satisfy the {err.validator or 'schema'} constraint."
        row={"code":code,"instance_location":_pointer(err.absolute_path),"schema_location":_pointer(err.absolute_schema_path),"problem":problem,"required_correction":correction,"expected":expected,"received":received}
        if branch: row["alternative"]=branch
        findings.append(row)
    # deterministic ordering and exact deduplication only
    uniq={}
    for row in findings:
        key=(row["instance_location"],row["schema_location"],row["code"],_stable(row))
        uniq[key]=row
    return [uniq[k] for k in sorted(uniq)]


def audit_json(raw, schema, validators=()):
    if not isinstance(raw,str):
        return None,{"phase":"syntax","valid":False,"findings":[{"code":"syntax.non_text","instance_location":"","schema_location":"","problem":"The model response is not text.","required_correction":"Return one complete JSON document as text."}]}
    try: value=json.loads(raw)
    except json.JSONDecodeError as exc:
        finding={"code":"syntax.invalid_json","instance_location":"","schema_location":"","problem":exc.msg,"required_correction":"Return syntactically valid JSON without changing the intended informational content.","line":exc.lineno,"column":exc.colno,"position":exc.pos}
        return None,{"phase":"syntax","valid":False,"findings":[finding]}
    except (TypeError,ValueError):
        return None,{"phase":"syntax","valid":False,"findings":[{"code":"syntax.invalid_json","instance_location":"","schema_location":"","problem":"The response could not be parsed as JSON.","required_correction":"Return one syntactically valid JSON document."}]}
    schema_findings=_schema_findings(value,schema)
    if schema_findings:
        return value,{"phase":"schema","valid":False,"findings":schema_findings}
    findings=[]
    for validator in validators or ():
        rows=validator(value) or []
        for row in rows:
            if not isinstance(row,dict): raise Fault("contract_validator_invalid")
            item={"code":row.get("code","contract.invalid"),"instance_location":row.get("instance_location",row.get("path","")),"schema_location":row.get("schema_location",""),"problem":row.get("problem",row.get("detail","The output violates a deterministic contract.")),"required_correction":row.get("required_correction","Correct this item while preserving unaffected supported content.")}
            for k in ("expected","received","ids","deferred","kind"):
                if k in row: item[k]=deepcopy(row[k])
            findings.append(item)
    uniq={}
    for row in findings:
        key=(str(row.get("instance_location","")),str(row.get("schema_location","")),str(row.get("code","")),_stable(row))
        uniq[key]=row
    findings=[uniq[k] for k in sorted(uniq)]
    if findings:
        kinds={str(row.get("kind") or "contract") for row in findings}
        phase=next(iter(kinds)) if len(kinds)==1 and next(iter(kinds)) in {"contract","reference","protocol"} else "contract"
        return value,{"phase":phase,"valid":False,"findings":findings}
    return value,{"phase":"complete","valid":True,"findings":[]}


def audit_fault(audit):
    phase=audit.get("phase")
    code={"syntax":"syntax_invalid","schema":"schema_invalid","contract":"reference_invalid","reference":"reference_invalid","protocol":"protocol_invalid"}.get(phase,"model_content_invalid")
    return Fault(code,phase,findings=deepcopy(audit.get("findings",[])))


def render_findings(findings):
    lines=[]
    for idx,row in enumerate(findings,1):
        where=row.get("instance_location") or "/"
        lines.append(f"{idx}. {row.get('code')} at {where}")
        lines.append(f"   Problem: {row.get('problem','')}")
        lines.append(f"   Required correction: {row.get('required_correction','')}")
        if "expected" in row: lines.append("   Expected: "+_stable(row["expected"]))
        if "received" in row: lines.append("   Received: "+_stable(row["received"]))
        if row.get("alternative"): lines.append("   Schema alternative: "+str(row["alternative"]))
    return "\n".join(lines)


def repair_feedback(*, phase, task_prompt, source_context, output_schema, allowed_actions, failed_raw, findings, repeat_count=0):
    preservation=("Preserve every unaffected supported fact, decision, identifier and reference from the failed response. "
                  "Do not invent source facts, weaken the output contract, or bypass later semantic checks.")
    if phase=="syntax":
        correction="Fix serialization only. Do not change informational content."
    else:
        correction="Correct every listed structural, reference, or protocol defect while keeping the original task and contract unchanged."
    if repeat_count:
        correction += " The previous repair repeated the same rejected defect; make a material correction to the cited findings."
    rendered=("The previous model response failed deterministic validation.\n\n"
              f"Audit phase: {phase}\n\nFindings:\n{render_findings(findings)}\n\n"
              f"Corrective instruction: {correction}\nPreservation instruction: {preservation}\n"
              "Return the complete output only, not a patch or commentary.")
    envelope={"kind":"output_repair","phase":phase,"original_task":task_prompt,"source_context":deepcopy(source_context),"unchanged_output_schema":deepcopy(output_schema),"allowed_actions":deepcopy(allowed_actions or []),"failed_raw_response":failed_raw,"findings":deepcopy(findings),"corrective_instruction":correction,"preservation_instruction":preservation,"complete_output_only":True}
    return rendered,envelope


def semantic_feedback(*, checker_id, target_id, failed_candidate, checker_findings, original_task, original_context, output_schema):
    rendered=(f"Mandatory checker {checker_id} found substantive defects in {target_id}. Fix every cited defect and preserve unaffected supported content. "
              "Do not weaken the task, source constraints, or output contract. Return a complete revised candidate.")
    return {"kind":"semantic_revision","checker_id":checker_id,"target_id":target_id,"failed_candidate":deepcopy(failed_candidate),"checker_findings":deepcopy(checker_findings),"original_task":original_task,"original_context":deepcopy(original_context),"unchanged_output_schema":deepcopy(output_schema),"rendered_feedback":rendered}
