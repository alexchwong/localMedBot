"""Application-neutral scheduler with durable model/tool journals and review gates."""
from __future__ import annotations
from copy import deepcopy
import json,time,threading,uuid
from .contracts import Fault, ModelSuspension, validate, validate_actor
from .compiler import compile_workflow,artifact_ref
from .knowledge import Knowledge
from .storage import uid
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
_INTEGRITY_CODES={"syntax_invalid","schema_invalid","reference_invalid","scope_denied","resume_contract_mismatch","conflict_extension_invalid","conflict_claim_mapping_invalid","checks_not_passed"}
_BUDGET_CODES={"budget_exhausted","agent_turn_limit"}
_CONFIG_CODES={"model_missing","endpoint_invalid","profile_invalid","profile_override_invalid","profile_workflow_mismatch","model_unavailable"}
_RECOVERABLE_PROVIDER={"credential_missing","authentication_rejected","endpoint_unreachable","provider_timeout","provider_transient_failure","provider_request_rejected","provider_incompatible_response","provider_redirect_rejected","output_truncated","external_response_unknown"}


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
        # target is usable only if its direct needs have complete artifacts.
        by={n["id"]:n for n in wf["nodes"]}
        targets=[t for t in targets if all(run["nodes"].get(x) in {"complete","skipped"} for x in by[t].get("needs",[]))]
    return {"category":category,"code":code,"node_id":node_id,"attempt":attempt,"human_revisable":bool(targets),"allowed_revision_targets":targets}


class Context:
    def __init__(self,runner,run,node,attempt):
        self.runner,self.store,self.run,self.node=runner,runner.store,run,node
        self.attempt,self.call_index,self.tool_index=attempt,0,0
        self.started=time.monotonic(); self.base_seconds=run["usage"]["seconds"]; self.base_node_seconds=run["node_usage"][node["id"]]["seconds"]
        self.knowledge=Knowledge(self.store,run["corpora"],run["snapshot"]["policy"].get("overlay"))
    def save(self):
        elapsed=time.monotonic()-self.started
        try:
            latest=self.store.run(self.run["id"]); self.run["cancel_requested"]=bool(self.run.get("cancel_requested") or latest.get("cancel_requested"))
        except Fault:
            pass
        self.run["usage"]["seconds"]=self.base_seconds+elapsed; self.run["node_usage"][self.node["id"]]["seconds"]=self.base_node_seconds+elapsed
        self.store.put_run(self.run)
    def event(self,kind,data): self.store.event(self.run["id"],kind,{"node":self.node["id"],"attempt":self.attempt,**data})
    def reserve(self,**amounts):
        self.save(); limits=self.run["snapshot"]["policy"]["limits"]
        for field in ["seconds",*amounts]:
            amount=amounts.get(field,0)
            if self.run["usage"][field]+amount>limits[field] or self.run["node_usage"][self.node["id"]][field]+amount>limits["node_"+field]: raise Fault("budget_exhausted",field)
        for f,v in amounts.items(): self.run["usage"][f]+=v; self.run["node_usage"][self.node["id"]][f]+=v
        self.save()
    def call(self,prompt,payload,schema,role="reasoning",allowed_actions=None): return self.runner.model_steps.call(self,prompt,payload,schema,role,allowed_actions)
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
    def create(self,snapshot,input_value,profile,corpora=None,rid=None,origins=None,derived_from_run_id=None):
        snapshot=compile_workflow(deepcopy(snapshot),self.registry); validate(input_value,snapshot.get("input_schema",{}))
        run={"id":rid or uid(),"status":"pending","run_contract_version":RUN_CONTRACT_VERSION,"step_contract_version":STEP_CONTRACT_VERSION,"input":deepcopy(input_value),"snapshot":snapshot,"profile":deepcopy(profile),
             "corpora":list(corpora or []),"nodes":{},"active":{},"attempts":{},"feedback":{},"cycles":{},"provider_offsets":{},
             "usage":{"turns":0,"tokens":0,"tool_calls":0,"seconds":0},"node_usage":{},"approval":None,"review_disposition":None,"omission_policy":{"extraction_revision":None,"omitted_fact_ids":[],"reasons":{},"review_event_id":None},
             "created":time.time(),"runtime_version":__version__,"origins":origins or {"task_input":"unknown","evidence":{},"revision_feedback":{}},"data_origin":aggregate_origins(origins or {"task_input":"unknown","evidence":{},"revision_feedback":{}}),"derived_from_run_id":derived_from_run_id,
             "block":None,"error":None,"waiting_request_id":None,"cancel_requested":False,"external_response_unknown":False,"check_outcomes":{},"evidence_outcome":None,"self_executor_label":"unreported" if profile.get("executor")=="self" else None}
        for n in snapshot["workflow"]["nodes"]: run["nodes"][n["id"]]="pending"; run["node_usage"][n["id"]]={"turns":0,"tokens":0,"tool_calls":0,"seconds":0}
        self.store.put_run(run); self.store.event(run["id"],"created",{"profile_id":profile["id"],"executor":profile["executor"],"corpora":run["corpora"],"run_contract_version":RUN_CONTRACT_VERSION})
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
    def _finish_node(self,run,node,payload,outcome):
        review=node.get("review")
        if review and payload.get("status")!="pass":
            count=run["cycles"].get(node["id"],0)
            if count>=review["max_revisions"]: raise Fault("semantic_check_exhausted",node["id"],payload.get("findings"))
            run["cycles"][node["id"]]=count+1; run["feedback"][review["target"]]=payload; self.invalidate(run,review["target"])
        if outcome=="waiting_review": self.ensure_checks(run); run["status"]="waiting_review"
        run.pop("postprocess",None)
    def _block_or_fail(self,run,ctx,exc):
        if exc.code in _RECOVERABLE_PROVIDER:
            run["status"]="failed"; run["error"]={"code":exc.code,"detail":exc.detail,"recovery_action":"resume"}; run["block"]=None
        else:
            block=block_for(exc,ctx.node["id"],ctx.attempt,run); run["status"]="blocked"; run["block"]=block; run["error"]={"code":exc.code,"detail":exc.detail}
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
                resuming=run["nodes"][nid]=="running"
                if not resuming: run["attempts"][nid]=run["attempts"].get(nid,0)+1
                run["nodes"][nid]="running"; self.store.put_run(run); ctx=Context(self,run,node,run["attempts"][nid])
                try:
                    inputs,refs=self._resolve_inputs(run,node); attempt_doc={"contract_version":STEP_CONTRACT_VERSION,"run_id":rid,"node_id":nid,"attempt":ctx.attempt,"resolved_inputs":deepcopy(inputs),"dependency_revisions":refs,"node_config":deepcopy(node.get("config",{})),"state":"running"}
                    old=self.store.step_attempt(rid,nid,ctx.attempt)
                    if old and json.dumps(old.get("resolved_inputs"),sort_keys=True)!=json.dumps(inputs,sort_keys=True): raise Fault("resume_contract_mismatch")
                    self.store.put_step_attempt(rid,nid,ctx.attempt,attempt_doc)
                    result=self.registry.get(node["module"]).execute(ctx,inputs,node.get("config",{})); validate(result.payload,node.get("schema",{})); self.model_steps.guard_persist(result.payload,run["profile"]); ctx.reserve()
                    if run.get("cancel_requested"):
                        run["status"]="cancelled"; run["approval"]=None; ctx.save(); self.store.event(rid,"cancelled",{"before_commit_node":nid}); return run
                    rev=self.store.commit(run,nid,result.payload,{"attempt":ctx.attempt,"inputs":refs,"schema":node.get("schema",{}),"corpora":list(run["corpora"])})
                    if isinstance(result.payload,dict) and "status" in result.payload and node["module"]=="content_check": run.setdefault("check_outcomes",{})[nid]=result.payload["status"]
                    if isinstance(result.payload,dict) and result.payload.get("evidence_outcome"): run["evidence_outcome"]=result.payload["evidence_outcome"]
                    attempt_doc["state"]="complete"; attempt_doc["artifact_revision"]=rev; self.store.put_step_attempt(rid,nid,ctx.attempt,attempt_doc); ctx.event("node_complete",{"revision":rev})
                    self._finish_node(run,node,result.payload,result.outcome); ctx.save()
                    if run["status"]=="waiting_review": return run
                except ModelSuspension as exc:
                    run["status"]="waiting_model"; run["waiting_request_id"]=exc.request_id; ctx.save(); return run
                except Fault as exc:
                    ctx.event("node_error",{"code":exc.code,"detail":exc.detail,"findings":exc.findings})
                    repairs=run.setdefault("repair_counts",{}).get(nid,0)
                    if exc.code in {"syntax_invalid","schema_invalid","reference_invalid","conflict_extension_invalid","conflict_claim_mapping_invalid"} and repairs<node.get("repairs",0):
                        run["repair_counts"][nid]=repairs+1; run["feedback"][nid]={"code":exc.code,"findings":exc.findings}; run["nodes"][nid]="pending"; ctx.save(); continue
                    return self._block_or_fail(run,ctx,exc)

    def resume(self,rid):
        with self.lock:
            run=self.store.run(rid); self._legacy_guard(run)
            if run["status"] in {"blocked","rejected","completed","waiting_review","cancelled"}: raise Fault("run_not_resumable")
            if run["status"]=="failed":
                code=(run.get("error") or {}).get("code")
                if code=="external_response_unknown": run["external_retry_authorized"]=True
                else: run["provider_retry_authorized"]=True
                run["status"]="pending"
                self.store.event(rid,"resume_requested",{"previous_error":code,"external_call_may_repeat":code=="external_response_unknown"})
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
            allowed_payload={
                "approve":common|{"acknowledged_omission_ids","acknowledged_conflict_ids"},
                "reject":common,
                "revise":common|{"target","omissions","expected_block"},
            }[decision]
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
                run["approval"]={k:v for k,v in event.items() if k!="canonical"}; run["status"]="completed"; run["review_disposition"]="approved"
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
                self.invalidate(run,target); run["feedback"][target]={"comments":comments}; run["origins"].setdefault("revision_feedback",{})[request_id]="user_supplied"; run["data_origin"]=aggregate_origins(run["origins"]); run["status"]="pending"
            self.store.put_review_event(rid,request_id,event); self.store.event(rid,"human_review",{k:v for k,v in event.items() if k!="canonical"}); self.store.put_run(run); return run
    def cancel(self,rid):
        run=self.store.run(rid); self._legacy_guard(run)
        if run["status"] in {"completed","rejected"}: raise Fault("run_not_active")
        run["cancel_requested"]=True
        if run["status"] not in {"running"}: run["status"]="cancelled"; run["approval"]=None
        self.store.put_run(run); self.store.event(rid,"cancel_requested",{})
