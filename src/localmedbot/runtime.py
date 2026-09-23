"""Application-neutral scheduler with durable model/tool journals and review gates."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import json,time,threading,uuid,re
from .contracts import Fault, ModelSuspension, validate, validate_actor
from .compiler import compile_workflow,artifact_ref
from .knowledge import Knowledge
from .storage import uid
from .audit import semantic_feedback
from .errors import present_error
from . import __version__, RUN_CONTRACT_VERSION, STEP_CONTRACT_VERSION


def aggregate_origins(origins):
    vals=[]
    task=origins.get("task_input")
    if task: vals.append(task)
    vals.extend(v for v in origins.get("evidence",{}).values() if v)
    vals.extend(v for v in origins.get("revision_feedback",{}).values() if v)
    if not vals or "unknown" in vals: return "unknown"
    known=set(vals)
    return "mixed" if len(known)>1 else next(iter(known))

def dig(value,path):
    for part in path.split(".") if path else []:
        if not isinstance(value,dict) or part not in value:return None
        value=value[part]
    return value

_BLOCK_PRECEDENCE={"integrity":5,"internal":4,"configuration":3,"budget":2,"content_revisable":1}
_CONTENT_CODES={"no_extractable_facts","semantic_check_exhausted"}
_INTEGRITY_CODES={"syntax_invalid","schema_invalid","reference_invalid","protocol_invalid","scope_denied","resume_contract_mismatch","conflict_extension_invalid","conflict_claim_mapping_invalid","checks_not_passed","storage_integrity_failure"}
_BUDGET_CODES={"budget_exhausted","agent_turn_limit","repair_context_too_large"}
_CONFIG_CODES={"model_missing","endpoint_invalid","profile_invalid","profile_override_invalid","profile_workflow_mismatch","model_unavailable","schema_definition_invalid"}
_RECOVERABLE_PROVIDER={"credential_missing","authentication_rejected","endpoint_unreachable","provider_timeout","provider_transient_failure","provider_request_rejected","provider_incompatible_response","provider_redirect_rejected","output_truncated","external_response_unknown"}
# Process interruption and unexpected termination are recoverable only through an
# explicit operator action; the runtime never continues them on its own.
_RECOVERABLE_EXECUTION={"execution_interrupted","execution_stopped","execution_dispatch_failed"}


def recovery_action(run):
    """Classify the single explicit recovery action permitted for a failed run."""
    if run.get("status")!="failed": return None
    code=(run.get("error") or {}).get("code")
    if code in _RECOVERABLE_PROVIDER or code in _RECOVERABLE_EXECUTION:
        return "explicit_external_retry" if code=="external_response_unknown" else "resume"
    return None


def block_for(exc,node_id,attempt,run):
    code=exc.code
    if code in _CONTENT_CODES: category="content_revisable"
    elif code in _INTEGRITY_CODES: category="integrity"
    elif code in _BUDGET_CODES: category="budget"
    elif code in _CONFIG_CODES: category="configuration"
    else: category="internal"
    targets=[]
    if category=="content_revisable":
        wf=run["snapshot"]["workflow"]
        if wf.get("revision_targets"): targets=list(wf["revision_targets"])
        elif wf.get("revision_target"): targets=[wf["revision_target"]]
        targets=[t for t in targets if t in run["nodes"]]
        by={n["id"]:n for n in wf["nodes"]}
        targets=[t for t in targets if all(run["nodes"].get(x) in {"complete","skipped"} for x in by[t].get("needs",[]))]
    return {"category":category,"code":code,"node_id":node_id,"attempt":attempt,"human_revisable":bool(targets),"allowed_revision_targets":targets}


def generated_run_id(workflow_id):
    safe=re.sub(r"[^A-Za-z0-9_.-]+","-",str(workflow_id)).strip("-") or "workflow"
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{safe}_{uuid.uuid4().hex[:8]}"


def _nonneg(value,name):
    if isinstance(value,bool) or not isinstance(value,int) or value<0: raise Fault("invalid_retry_limit",name)
    return value


def resolve_retry_limits(snapshot,overrides=None):
    defaults=deepcopy(snapshot.get("execution_defaults") or {})
    out_default=_nonneg(defaults.get("output_repair_retries",3),"output_repair_retries")
    sem_default=_nonneg(defaults.get("semantic_revision_retries",3),"semantic_revision_retries")
    policy=snapshot.get("policy",{})
    if "output_repair_retries" in policy: out_default=_nonneg(policy["output_repair_retries"],"output_repair_retries")
    if "semantic_revision_retries" in policy: sem_default=_nonneg(policy["semantic_revision_retries"],"semantic_revision_retries")
    overrides=overrides or {}
    dev_out=overrides.get("output_repair_retries")
    dev_sem=overrides.get("semantic_revision_retries")
    if dev_out is not None: out_default=_nonneg(dev_out,"output_repair_retries")
    if dev_sem is not None: sem_default=_nonneg(dev_sem,"semantic_revision_retries")
    by_node={}; by_check={}
    for n in snapshot["workflow"]["nodes"]:
        v=out_default
        if dev_out is None and "repairs" in n: v=_nonneg(n["repairs"],f"{n['id']}.repairs")
        by_node[n["id"]]=v
        if n.get("review"):
            sv=sem_default
            if dev_sem is None and "max_revisions" in n["review"]: sv=_nonneg(n["review"]["max_revisions"],f"{n['id']}.max_revisions")
            by_check[n["id"]]=sv
    return {"output_repair_default":out_default,"semantic_revision_default":sem_default,"output_repair_by_node":by_node,"semantic_revision_by_check":by_check,"developer_overrides":{"output_repair_retries":dev_out,"semantic_revision_retries":dev_sem}}


class Context:
    def __init__(self,runner,run,node,attempt):
        self.runner,self.store,self.run,self.node=runner,runner.store,run,node
        self.attempt,self.turn_index,self.tool_index=attempt,0,0
        self.started=time.monotonic(); self.base_seconds=run["usage"]["seconds"]; self.base_node_seconds=run["node_usage"][node["id"]]["seconds"]
        self.knowledge=Knowledge(self.store,run["corpora"],run["snapshot"]["policy"].get("overlay"))
    def save(self):
        elapsed=time.monotonic()-self.started
        try:
            latest=self.store.run(self.run["id"]); self.run["cancel_requested"]=bool(self.run.get("cancel_requested") or latest.get("cancel_requested"))
        except Fault: pass
        self.run["usage"]["seconds"]=self.base_seconds+elapsed; self.run["node_usage"][self.node["id"]]["seconds"]=self.base_node_seconds+elapsed
        self.base_seconds=self.run["usage"]["seconds"]; self.base_node_seconds=self.run["node_usage"][self.node["id"]]["seconds"]; self.started=time.monotonic()
        self.store.put_run(self.run)
    def event(self,kind,data): self.store.event(self.run["id"],kind,{"node":self.node["id"],"attempt":self.attempt,**data})
    def reserve(self,**amounts):
        self.save(); limits=self.run["snapshot"]["policy"]["limits"]
        for field in ["seconds",*amounts]:
            amount=amounts.get(field,0)
            if self.run["usage"][field]+amount>limits[field] or self.run["node_usage"][self.node["id"]][field]+amount>limits["node_"+field]: raise Fault("budget_exhausted",field)
        for f,v in amounts.items(): self.run["usage"][f]+=v; self.run["node_usage"][self.node["id"]][f]+=v
        self.save()
    def call(self,prompt,payload,schema,role="reasoning",allowed_actions=None,validators=None): return self.runner.model_steps.call(self,prompt,payload,schema,role,allowed_actions,validators)
    def tool(self,name,args):
        permitted=set(self.run["snapshot"]["policy"].get("tools",[]))&set(self.node.get("config",{}).get("tools",[]))
        if name not in permitted: raise Fault("tool_denied",name)
        if not isinstance(args,dict): raise Fault("tool_arguments")
        self.tool_index+=1; canonical=json.dumps({"tool":name,"arguments":args},sort_keys=True,separators=(",",":"),ensure_ascii=False)
        existing=self.store.put_tool_call(self.run["id"],self.node["id"],self.attempt,self.tool_index,{"canonical":canonical,"tool":name,"arguments":args,"status":"pending"})
        if existing.get("status")=="complete": self.event("tool_replayed",{"tool":name,"tool_index":self.tool_index}); return existing["result"]
        self.reserve(tool_calls=1); self.event("tool_request",{"tool":name,"arguments":args,"tool_index":self.tool_index})
        if name=="evidence.search":
            if set(args)-{"query","filters","limit","mode"}: raise Fault("tool_arguments")
            if args.get("limit",12)>12: raise Fault("tool_arguments")
            result=self.knowledge.search(**args)
        elif name=="evidence.read":
            if set(args)!={"corpus_id","evidence_id"}: raise Fault("tool_arguments")
            result=self.knowledge.read(args["corpus_id"],args["evidence_id"])
        else: raise Fault("tool_denied",name)
        doc={"canonical":canonical,"tool":name,"arguments":args,"status":"complete","result":result}; self.store.update_tool_call(self.run["id"],self.node["id"],self.attempt,self.tool_index,doc)
        self.event("tool_result",{"tool":name,"result":result,"tool_index":self.tool_index}); return result


class Runner:
    def __init__(self,store,registry,model_steps): self.store,self.registry,self.model_steps=store,registry,model_steps; self.lock=threading.RLock()
    def create(self,snapshot,input_value,profile,corpora=None,rid=None,origins=None,derived_from_run_id=None,retry_overrides=None,title=None):
        snapshot=compile_workflow(deepcopy(snapshot),self.registry); validate(input_value,snapshot.get("input_schema",{}))
        if rid is None:
            for _ in range(10):
                candidate=generated_run_id(snapshot["manifest"]["id"])
                if not self.store.run_dir(candidate).exists(): rid=candidate; break
            if rid is None: raise Fault("run_id_conflict")
        retry_limits=resolve_retry_limits(snapshot,retry_overrides)
        run={"id":rid,"status":"pending","run_contract_version":RUN_CONTRACT_VERSION,"step_contract_version":STEP_CONTRACT_VERSION,"input":deepcopy(input_value),"snapshot":snapshot,"profile":deepcopy(profile),
             "corpora":list(corpora or []),"nodes":{},"active":{},"attempts":{},"feedback":{},"cycles":{},"semantic_episodes":{},"provider_offsets":{},"retry_limits":retry_limits,"repair_counts":{},
             "usage":{"turns":0,"tokens":0,"tool_calls":0,"seconds":0},"node_usage":{},"approval":None,"review_disposition":None,"omission_policy":{"extraction_revision":None,"omitted_fact_ids":[],"reasons":{},"review_event_id":None},
             "created":time.time(),"title":title,"runtime_version":__version__,"origins":origins or {"task_input":"unknown","evidence":{},"revision_feedback":{}},"data_origin":aggregate_origins(origins or {"task_input":"unknown","evidence":{},"revision_feedback":{}}),"derived_from_run_id":derived_from_run_id,
             "block":None,"error":None,"waiting_request_id":None,"cancel_requested":False,"external_response_unknown":False,"check_outcomes":{},"evidence_outcome":None,"self_executor_label":"unreported" if profile.get("executor")=="self" else None}
        for n in snapshot["workflow"]["nodes"]: run["nodes"][n["id"]]="pending"; run["node_usage"][n["id"]]={"turns":0,"tokens":0,"tool_calls":0,"seconds":0}
        self.store.create_run(run); self.store.event(run["id"],"created",{"profile_id":profile["id"],"executor":profile["executor"],"corpora":run["corpora"],"run_contract_version":RUN_CONTRACT_VERSION,"retry_limits":retry_limits})
        return run["id"]
    def _legacy_guard(self,run):
        if run.get("legacy") or "run_contract_version" not in run: raise Fault("legacy_run_read_only")
        if run.get("run_contract_version")!=RUN_CONTRACT_VERSION: raise Fault("run_contract_version_mismatch")
    def resolve(self,run,ref):
        if ref=="run.input": return run["input"]
        if ref.startswith("run."): return dig(run,ref[4:])
        if ref.startswith("feedback."): return run["feedback"].get(ref.split(".",1)[1])
        if ref.startswith("artifacts."):
            parent,path=artifact_ref(ref,run["nodes"])
            if parent in run["active"]: return dig(self.store.artifact(run["id"],parent,run["active"][parent])["payload"],path)
        return None
    def invalidate(self,run,target):
        invalid={target}
        for n in run["snapshot"]["workflow"]["nodes"]:
            if n["id"]==target or invalid&set(n.get("needs",[])):
                invalid.add(n["id"]); run["active"].pop(n["id"],None); run["nodes"][n["id"]]="pending"
        run["approval"]=None; run["review_disposition"]=None; run["block"]=None; run["error"]=None; run["waiting_request_id"]=None
        self.store.event(run["id"],"invalidated",{"nodes":sorted(invalid)})
    def ensure_checks(self,run):
        for nid in run["snapshot"]["policy"].get("required_checks",[]):
            if nid not in run["active"] or self.store.artifact(run["id"],nid,run["active"][nid])["payload"].get("status")!="pass": raise Fault("checks_not_passed",nid)
    def _resolve_inputs(self,run,node):
        inputs={}; refs={}
        for name,b in node.get("inputs",{}).items():
            spec=b if isinstance(b,dict) else {"from":b}; value=self.resolve(run,spec["from"])
            if value is None and not spec.get("optional"): raise Fault("required_input_missing",name)
            inputs[name]=value
            if spec["from"].startswith("artifacts."):
                parent=artifact_ref(spec["from"],run["nodes"])[0]; refs[parent]=run["active"].get(parent)
        if node["id"] in run["feedback"]: inputs["revision_feedback"]=run["feedback"][node["id"]]
        validate(inputs,node.get("input_schema",{})); return inputs,refs
    def _semantic_revision(self,run,node,payload):
        review=node["review"]; checker=node["id"]; target=review["target"]
        limit=int(run.get("retry_limits",{}).get("semantic_revision_by_check",{}).get(checker,0))
        episode=int(run.setdefault("semantic_episodes",{}).get(checker,1)); key=f"{checker}:{target}:{episode}"
        count=int(run["cycles"].get(key,0))
        target_attempt=run.get("attempts",{}).get(target,0); old=self.store.step_attempt(run["id"],target,target_attempt) or {}
        failed=self.store.artifact(run["id"],target,run["active"][target])["payload"] if target in run.get("active",{}) else None
        target_node=next(n for n in run["snapshot"]["workflow"]["nodes"] if n["id"]==target)
        envelope=semantic_feedback(checker_id=checker,target_id=target,failed_candidate=failed,checker_findings=payload.get("findings",[]),original_task=target_node.get("config",{}).get("prompt",target_node.get("module")),original_context=old.get("resolved_inputs",{}),output_schema=target_node.get("schema",{}))
        link={"link_id":uid(),"kind":"automatic","run_id":run["id"],"checker_id":checker,"checker_attempt":run.get("attempts",{}).get(checker,0),"checker_artifact_revision":run.get("active",{}).get(checker),"target_id":target,"failed_target_attempt":target_attempt,"failed_target_revision":run.get("active",{}).get(target),"episode":episode,"revision_ordinal":count+1,"frozen_limit":limit,"findings":deepcopy(payload.get("findings",[])),"feedback_envelope":deepcopy(envelope),"rendered_feedback":envelope["rendered_feedback"],"next_target_attempt":target_attempt+1 if count<limit else None,"next_operation_id":None,"outcome":"pending" if count<limit else "exhausted","created":time.time()}
        if count>=limit:
            link["revision_ordinal"]=count; self.store.put_semantic_revision(link); raise Fault("semantic_check_exhausted",checker,payload.get("findings"))
        self.store.put_semantic_revision(link); run["cycles"][key]=count+1; run["feedback"][target]=deepcopy(envelope); self.invalidate(run,target); self.store.event(run["id"],"semantic_revision",{"link_id":link["link_id"],"checker":checker,"target":target,"ordinal":count+1,"limit":limit})
    def _finish_node(self,run,node,payload,outcome):
        review=node.get("review")
        if review:
            # Close the semantic link whose regenerated target this checker just assessed.
            pending=[x for x in self.store.semantic_revisions(run["id"]) if x.get("checker_id")==node["id"] and x.get("outcome")=="in_progress"]
            if pending:
                link=pending[-1]
                link["outcome"]="corrected" if payload.get("status")=="pass" else "still_invalid"
                link["result_checker_attempt"]=run.get("attempts",{}).get(node["id"])
                link["result_checker_revision"]=run.get("active",{}).get(node["id"]); link["updated"]=time.time()
                self.store.update_semantic_revision(link["link_id"],link)
            if payload.get("status")!="pass": self._semantic_revision(run,node,payload)
        if outcome=="waiting_review": self.ensure_checks(run); run["status"]="waiting_review"
        run.pop("postprocess",None)
    def _set_attempt_state(self,run,node_id,attempt,state,**extra):
        doc=self.store.step_attempt(run["id"],node_id,attempt)
        if doc:
            doc["state"]=state; doc.update(extra); self.store.put_step_attempt(run["id"],node_id,attempt,doc)
    def _block_or_fail(self,run,ctx,exc):
        presentation=present_error(exc,stage=ctx.node["id"],attempt=ctx.attempt)
        if exc.code in _RECOVERABLE_PROVIDER:
            run["status"]="failed"; run["error"]={**presentation,"recovery_action":"explicit_external_retry" if exc.code=="external_response_unknown" else "resume"}; run["block"]=None; self._set_attempt_state(run,ctx.node["id"],ctx.attempt,"failed",error=deepcopy(run["error"])); run["nodes"][ctx.node["id"]]="failed"
        else:
            block=block_for(exc,ctx.node["id"],ctx.attempt,run); run["status"]="blocked"; run["block"]=block; run["error"]=presentation; self._set_attempt_state(run,ctx.node["id"],ctx.attempt,"blocked",error=deepcopy(run["error"])); run["nodes"][ctx.node["id"]]="blocked"
        ctx.save(); return run
    def advance(self,rid):
        with self.lock:
            run=self.store.run(rid); self._legacy_guard(run)
            if run.get("runtime_version")!=__version__: raise Fault("runtime_version_mismatch")
            if run["status"] in {"completed","waiting_review","rejected","cancelled","blocked"}: return run
            run["status"]="running"; run["waiting_request_id"]=None; self.store.put_run(run); nodes=run["snapshot"]["workflow"]["nodes"]
            while True:
                latest=self.store.run(rid); run["cancel_requested"]=bool(run.get("cancel_requested") or latest.get("cancel_requested"))
                if run.get("cancel_requested"):
                    run["status"]="cancelled"; run["approval"]=None; self.store.put_run(run); self.store.event(rid,"cancelled",{}); return run
                pending=[n for n in nodes if run["nodes"][n["id"]] not in {"complete","skipped"}]
                if not pending:
                    if run["snapshot"]["policy"].get("human_approval") and not run["approval"]: self.ensure_checks(run); run["status"]="waiting_review"
                    else: run["status"]="completed"
                    self.store.put_run(run); return run
                node=pending[0]; nid=node["id"]
                cond=node.get("when")
                if cond:
                    v=self.resolve(run,cond["from"]); applies=v is not None if cond["op"]=="exists" else v==cond.get("value") if cond["op"]=="equals" else v in cond.get("value",[])
                    if not applies: run["nodes"][nid]="skipped"; self.store.put_run(run); continue
                resuming=run["nodes"][nid] in {"running","waiting_self","retrying"}
                if not resuming: run["attempts"][nid]=run["attempts"].get(nid,0)+1
                run["nodes"][nid]="running"; self.store.put_run(run); ctx=Context(self,run,node,run["attempts"][nid])
                try:
                    inputs,refs=self._resolve_inputs(run,node); attempt_doc={"stage_attempt_id":f"{rid}:{nid}:{ctx.attempt}","display_ordinal":ctx.attempt,"contract_version":STEP_CONTRACT_VERSION,"run_id":rid,"node_id":nid,"attempt":ctx.attempt,"resolved_inputs":deepcopy(inputs),"dependency_revisions":refs,"node_config":deepcopy(node.get("config",{})),"state":"executing","started":time.time()}
                    old=self.store.step_attempt(rid,nid,ctx.attempt)
                    if old and json.dumps(old.get("resolved_inputs"),sort_keys=True)!=json.dumps(inputs,sort_keys=True): raise Fault("resume_contract_mismatch")
                    if old: attempt_doc={**old,"state":"executing"}
                    self.store.put_step_attempt(rid,nid,ctx.attempt,attempt_doc)
                    if not old: ctx.event("node_start",{"node_id":nid,"attempt":ctx.attempt})
                    result=self.registry.get(node["module"]).execute(ctx,inputs,node.get("config",{})); validate(result.payload,node.get("schema",{})); self.model_steps.guard_persist(result.payload,run["profile"]); ctx.reserve()
                    if run.get("cancel_requested"):
                        run["status"]="cancelled"; run["approval"]=None; self._set_attempt_state(run,nid,ctx.attempt,"cancelled"); ctx.save(); self.store.event(rid,"cancelled",{"before_commit_node":nid}); return run
                    rev=self.store.commit(run,nid,result.payload,{"attempt":ctx.attempt,"inputs":refs,"schema":node.get("schema",{}),"corpora":list(run["corpora"])})
                    if isinstance(result.payload,dict) and "status" in result.payload and node["module"]=="content_check": run.setdefault("check_outcomes",{})[nid]=result.payload["status"]
                    if isinstance(result.payload,dict) and result.payload.get("evidence_outcome"): run["evidence_outcome"]=result.payload["evidence_outcome"]
                    attempt_doc=self.store.step_attempt(rid,nid,ctx.attempt) or attempt_doc; attempt_doc["state"]="completed"; attempt_doc["artifact_revision"]=rev; attempt_doc["completed"]=time.time(); self.store.put_step_attempt(rid,nid,ctx.attempt,attempt_doc); ctx.event("node_complete",{"revision":rev})
                    self._finish_node(run,node,result.payload,result.outcome); ctx.save()
                    if run["status"]=="waiting_review": return run
                except ModelSuspension as exc:
                    run["status"]="waiting_model"; run["waiting_request_id"]=exc.request_id; run["nodes"][nid]="waiting_self"; self._set_attempt_state(run,nid,ctx.attempt,"waiting_self",waiting_request_id=exc.request_id); ctx.save(); return run
                except Fault as exc:
                    ctx.event("node_error",{"code":exc.code,"detail":exc.detail,"findings":exc.findings})
                    return self._block_or_fail(run,ctx,exc)

    def resume(self,rid,acknowledge_external_retry=False):
        with self.lock:
            run=self.store.run(rid); self._legacy_guard(run)
            if run["status"] in {"blocked","rejected","completed","waiting_review","cancelled"}: raise Fault("run_not_resumable")
            if run["status"]=="failed":
                code=(run.get("error") or {}).get("code"); action=recovery_action(run)
                if action is None: raise Fault("run_not_resumable")
                if action=="explicit_external_retry":
                    if acknowledge_external_retry is not True: raise Fault("external_retry_acknowledgement_required")
                    run["external_retry_authorized"]=True
                elif code in _RECOVERABLE_EXECUTION:
                    # Only authorize a repeat when an interrupted execution left a request
                    # that had already failed at the provider without being re-driven.
                    if any(r.get("status") in {"provider_error","setup_error"} for r in self.store.model_calls(rid)): run["provider_retry_authorized"]=True
                else: run["provider_retry_authorized"]=True
                node=(run.get("error") or {}).get("stage")
                if node and run["nodes"].get(node)=="failed": run["nodes"][node]="running"
                run["status"]="pending"
                self.store.event(rid,"resume_requested",{"previous_error":code,"external_call_may_repeat":code=="external_response_unknown","recovery_action":action})
                self.store.put_run(run)
        return self.advance(rid)
    def review(self,rid,payload):
        with self.lock:
            run=self.store.run(rid); self._legacy_guard(run); request_id=payload.get("review_request_id")
            try: uuid.UUID(request_id)
            except Exception: raise Fault("review_request_invalid")
            prior=self.store.review_event(rid,request_id)
            can=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(",",":"))
            if prior:
                if prior["canonical"]!=can: raise Fault("review_request_conflict")
                return run
            actor=validate_actor(payload.get("actor")); decision=payload.get("decision"); comments=payload.get("comments","")
            if decision not in {"approve","reject","revise"}: raise Fault("review_action")
            common={"review_request_id","revision","actor","decision","comments"}
            allowed_payload={"approve":common|{"acknowledged_omission_ids","acknowledged_conflict_ids"},"reject":common,"revise":common|{"target","omissions","expected_block"}}[decision]
            if set(payload)-allowed_payload: raise Fault("review_payload_invalid")
            if not isinstance(comments,str): raise Fault("review_payload_invalid")
            output=run["snapshot"]["workflow"]["output"]; active_rev=run["active"].get(output); revision=payload.get("revision")
            allowed={"waiting_review":{"approve","reject","revise"},"completed":{"reject","revise"},"rejected":{"revise"},"blocked":{"revise"}}
            if decision not in allowed.get(run["status"],set()): raise Fault("not_reviewable" if run["status"]!="rejected" else "review_revision_required")
            if run["status"]=="blocked":
                block=run.get("block") or {}
                if not block.get("human_revisable"): raise Fault("not_reviewable")
                if payload.get("expected_block")!={k:block.get(k) for k in ["code","node_id","attempt"]}: raise Fault("stale_review")
                if active_rev is None:
                    if revision is not None: raise Fault("stale_review")
                elif revision!=active_rev: raise Fault("stale_review")
            elif revision!=active_rev: raise Fault("stale_review")
            if decision in {"approve","reject"}: self.ensure_checks(run)
            event={"canonical":can,"review_request_id":request_id,"revision":revision,"actor":actor,"decision":decision,"comments":comments,"time":time.time()}
            if decision=="approve":
                if run["status"]=="rejected": raise Fault("review_revision_required")
                omit=set(run.get("omission_policy",{}).get("omitted_fact_ids",[])); ack=set(payload.get("acknowledged_omission_ids",[]))
                if omit!=ack: raise Fault("omission_acknowledgement_required")
                out=self.store.artifact(rid,output,active_rev)["payload"]; conflicts=set(out.get("conflict_ids",[])); cack=set(payload.get("acknowledged_conflict_ids",[]))
                if conflicts!=cack: raise Fault("conflict_acknowledgement_required")
                run["approval"]={k:v for k,v in event.items() if k!="canonical"}; run["status"]="completed"; run["review_disposition"]="approved"; run["error"]=None; run["block"]=None
            elif decision=="reject":
                run["approval"]=None; run["status"]="rejected"; run["review_disposition"]="rejected"
            else:
                target=payload.get("target") or run["snapshot"]["workflow"].get("revision_target")
                if run["status"]=="blocked" and target not in run["block"].get("allowed_revision_targets",[]): raise Fault("revision_target_invalid")
                if target not in (run["snapshot"]["workflow"].get("revision_targets") or [run["snapshot"]["workflow"].get("revision_target")]): raise Fault("revision_target_invalid")
                if run["snapshot"]["manifest"]["id"]=="clinical_letter":
                    omissions=payload.get("omissions")
                    if target=="facts": run["omission_policy"]={"extraction_revision":None,"omitted_fact_ids":[],"reasons":{},"review_event_id":None}
                    elif omissions is not None:
                        facts=self.store.artifact(rid,"facts",run["active"]["facts"]); fmap={f["id"] for f in facts["payload"]["facts"]}; reasons={}; ids=[]
                        if not isinstance(omissions,list): raise Fault("omission_invalid")
                        for o in omissions:
                            if not isinstance(o,dict) or set(o)!={"fact_id","reason","extraction_revision"} or o.get("fact_id") not in fmap or o.get("extraction_revision")!=facts["revision"] or not isinstance(o.get("reason"),str) or not o["reason"].strip(): raise Fault("omission_invalid")
                            ids.append(o["fact_id"]); reasons[o["fact_id"]]=o["reason"].strip()
                        run["omission_policy"]={"extraction_revision":facts["revision"],"omitted_fact_ids":sorted(set(ids)),"reasons":reasons,"review_event_id":request_id}
                for checker,n in [(n["id"],n) for n in run["snapshot"]["workflow"]["nodes"] if n.get("review",{}).get("target")==target]: run.setdefault("semantic_episodes",{})[checker]=int(run.get("semantic_episodes",{}).get(checker,1))+1
                self.invalidate(run,target); run["feedback"][target]={"kind":"human_revision","comments":comments}; run["origins"].setdefault("revision_feedback",{})[request_id]="user_supplied"; run["data_origin"]=aggregate_origins(run["origins"]); run["status"]="pending"
            self.store.put_review_event(rid,request_id,event); self.store.event(rid,"human_review",{k:v for k,v in event.items() if k!="canonical"}); self.store.put_run(run); return run
    def cancel(self,rid):
        run=self.store.run(rid); self._legacy_guard(run)
        if run["status"] in {"completed","rejected"}: raise Fault("run_not_active")
        run["cancel_requested"]=True
        if run["status"] not in {"running"}: run["status"]="cancelled"; run["approval"]=None
        for node,state in list(run.get("nodes",{}).items()):
            if state in {"running","waiting_self","retrying"}: run["nodes"][node]="cancelled"; self._set_attempt_state(run,node,run.get("attempts",{}).get(node,0),"cancelled")
        self.store.put_run(run); self.store.event(rid,"cancel_requested",{})
