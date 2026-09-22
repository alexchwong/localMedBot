"""Shared durable model-operation, request, physical-call and output-repair contract."""
from __future__ import annotations
from copy import deepcopy
import hashlib,json,time
from .contracts import Fault, ModelSuspension
from .providers import make_provider
from .profiles import classify_destination
from .audit import audit_json,audit_fault,repair_feedback
from .errors import present_error
from .storage import uid
from . import STEP_CONTRACT_VERSION


def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def _safe_usage_meta(response):
    return {k:deepcopy(v) for k,v in response.items() if k!="text"}


class ModelSteps:
    def __init__(self,store,profiles): self.store,self.profiles=store,profiles

    def build_messages(self,prompt,payload,schema):
        return [{"role":"system","content":prompt+"\nReturn JSON matching this schema:\n"+json.dumps(schema,ensure_ascii=False,sort_keys=True)},
                {"role":"user","content":json.dumps(payload,ensure_ascii=False,sort_keys=True)}]

    def build_repair_messages(self,base,raw,rendered,envelope):
        return [deepcopy(base[0]),deepcopy(base[1]),{"role":"assistant","content":raw},{"role":"user","content":rendered+"\n\nRepair envelope:\n"+json.dumps(envelope,ensure_ascii=False,sort_keys=True)}]

    def parse_text(self,text,schema):
        value,audit=audit_json(text,schema)
        if not audit["valid"]: raise audit_fault(audit)
        return value

    def _operation(self,ctx):
        existing=self.store.operation_for_attempt(ctx.run["id"],ctx.node["id"],ctx.attempt)
        if existing: return existing
        limit=int(ctx.run.get("retry_limits",{}).get("output_repair_by_node",{}).get(ctx.node["id"],0))
        doc={"operation_id":uid(),"run_id":ctx.run["id"],"node_id":ctx.node["id"],"attempt":ctx.attempt,"display_ordinal":ctx.attempt,"output_repair_limit":limit,"created":time.time(),"status":"active"}
        doc=self.store.put_operation(doc)
        for link in self.store.semantic_revisions(ctx.run["id"]):
            if link.get("target_id")==ctx.node["id"] and link.get("next_target_attempt")==ctx.attempt and not link.get("next_operation_id"):
                link["next_operation_id"]=doc["operation_id"]; link["outcome"]="in_progress"; self.store.update_semantic_revision(link["link_id"],link)
        return doc

    def _requests_for_operation(self,run_id,operation_id):
        return [r for r in self.store.model_calls(run_id) if r.get("operation_id")==operation_id]

    def _allocate_request(self,ctx,operation,turn_index,prompt,payload,schema,role,allowed_actions,purpose="initial",repair_ordinal=0,request_id=None,messages=None,feedback=None,repair_id=None):
        rows=self._requests_for_operation(ctx.run["id"],operation["operation_id"]); index=max([int(r.get("call_index",0)) for r in rows] or [0])+1
        settings=ctx.run["profile"]["effective_roles"].get(role)
        if settings is None: raise Fault("profile_role_missing",role)
        base_messages=self.build_messages(prompt,payload,schema)
        actual_messages=messages or base_messages
        logical={"contract_version":STEP_CONTRACT_VERSION,"run_id":ctx.run["id"],"node_id":ctx.node["id"],"attempt":ctx.attempt,"operation_id":operation["operation_id"],"turn_index":turn_index,"call_index":index,"purpose":purpose,"repair_ordinal":repair_ordinal,
                 "role":role,"workflow_id":ctx.run["snapshot"]["manifest"]["id"],"workflow_version":ctx.run["snapshot"]["workflow"].get("version"),"messages":deepcopy(actual_messages),"base_messages":deepcopy(base_messages),"output_schema":deepcopy(schema),"allowed_actions":deepcopy(allowed_actions or []),"effective_model":deepcopy(settings),
                 "evidence_scope":{"corpus_ids":list(ctx.run["corpora"])},"limits":deepcopy(ctx.run["snapshot"]["policy"]["limits"]),"repair_feedback":feedback,"repair_id":repair_id,
                 "task_prompt":prompt,"source_context":deepcopy(payload)}
        can=canonical({k:v for k,v in logical.items() if k not in {"call_index"}})
        reservation=len(json.dumps(actual_messages,ensure_ascii=False).encode("utf-8"))+int(settings.get("max_tokens",4096))
        rec={**logical,"request_id":request_id or uid(),"resolved_node_input_ref":f"{ctx.run['id']}:{ctx.node['id']}:{ctx.attempt}","status":"pending","canonical":can,"raw_response":None,"parsed":None,"audit":None,"findings":[],"response_metadata":None,"reservation":reservation,"created":time.time()}
        rec=self.store.put_model_call(rec); ctx.reserve(turns=1,tokens=reservation)
        return rec

    def _initial_request(self,ctx,operation,turn_index,prompt,payload,schema,role,allowed_actions):
        same=[r for r in self._requests_for_operation(ctx.run["id"],operation["operation_id"]) if int(r.get("turn_index",0))==turn_index]
        if same:
            first=same[0]
            settings=ctx.run["profile"]["effective_roles"].get(role)
            expected={"contract_version":STEP_CONTRACT_VERSION,"run_id":ctx.run["id"],"node_id":ctx.node["id"],"attempt":ctx.attempt,"operation_id":operation["operation_id"],"turn_index":turn_index,"purpose":"initial" if turn_index==1 else "continuation","repair_ordinal":0,"role":role,"workflow_id":ctx.run["snapshot"]["manifest"]["id"],"workflow_version":ctx.run["snapshot"]["workflow"].get("version"),"messages":self.build_messages(prompt,payload,schema),"base_messages":self.build_messages(prompt,payload,schema),"output_schema":schema,"allowed_actions":allowed_actions or [],"effective_model":settings,"evidence_scope":{"corpus_ids":list(ctx.run["corpora"])},"limits":ctx.run["snapshot"]["policy"]["limits"],"repair_feedback":None,"repair_id":None,"task_prompt":prompt,"source_context":payload}
            can=canonical(expected)
            if first.get("canonical")!=can: raise Fault("resume_contract_mismatch")
            return first
        purpose="initial" if turn_index==1 else "continuation"
        return self._allocate_request(ctx,operation,turn_index,prompt,payload,schema,role,allowed_actions,purpose=purpose)

    def call(self,ctx,prompt,payload,schema,role="reasoning",allowed_actions=None,validators=None):
        ctx.turn_index+=1; operation=self._operation(ctx); turn=ctx.turn_index
        request=self._initial_request(ctx,operation,turn,prompt,payload,schema,role,allowed_actions)
        while True:
            request=self.store.model_call(request["request_id"])
            if request["status"]=="complete":
                ctx.event("response_replayed",{"request_id":request["request_id"],"operation_id":operation["operation_id"]}); return request["parsed"]
            if request["status"]=="invalid":
                repairs=[r for r in self.store.repairs_for_operation(operation["operation_id"]) if r.get("failed_call_id")==request["request_id"]]
                if repairs:
                    link=repairs[-1]
                    if link.get("next_request_id"):
                        request=self.store.model_call(link["next_request_id"]); continue
                raise audit_fault(request.get("audit") or {"phase":"contract","findings":request.get("findings",[])})
            if request["status"]=="submitted":
                value,audit=audit_json(request.get("raw_response"),request["output_schema"],validators or ())
                request["audit"]=audit; request["findings"]=deepcopy(audit["findings"])
                if audit["valid"]:
                    request["parsed"]=value; request["status"]="complete"; request["completed"]=time.time(); self.store.update_model_call(request["request_id"],request)
                    if request.get("repair_id"):
                        link=self.store.repair(request["repair_id"]); link["outcome"]="corrected"; link["completed_request_id"]=request["request_id"]; self.store.update_repair(link["repair_id"],link)
                    op=self.store.operation_for_attempt(ctx.run["id"],ctx.node["id"],ctx.attempt); op["status"]="active"; self.store.update_operation(op["operation_id"],op)
                    return value
                request["status"]="invalid"; request["completed"]=time.time(); self.store.update_model_call(request["request_id"],request)
                if request.get("repair_id"):
                    link=self.store.repair(request["repair_id"]); link["outcome"]="still_invalid"; self.store.update_repair(link["repair_id"],link)
                request=self._repair_or_exhaust(ctx,operation,request,prompt,payload,schema,role,allowed_actions,audit); continue
            if request["status"] in {"dispatching","dispatch_unknown"}:
                if request["status"]=="dispatching":
                    request["status"]="dispatch_unknown"; request["error"]=present_error({"code":"external_response_unknown"},stage=ctx.node["id"],attempt=ctx.attempt); self.store.update_model_call(request["request_id"],request)
                if not ctx.run.pop("external_retry_authorized",False): raise Fault("external_response_unknown")
                request["status"]="pending"; request.pop("error",None); request["manual_retry"]=True; self.store.update_model_call(request["request_id"],request); ctx.event("external_response_retry",{"request_id":request["request_id"],"provider_may_bill_twice":True})
            if request["status"] in {"provider_error","setup_error"}:
                if request["status"]=="provider_error" and not ctx.run.pop("provider_retry_authorized",False):
                    err=request.get("error") or {}; raise Fault(err.get("code","provider_request_rejected"),err.get("detail",""),err.get("findings"))
                request["status"]="pending"; request["manual_retry"]=True; self.store.update_model_call(request["request_id"],request)
            if request["status"]!="pending": raise Fault("model_request_state_invalid",request["status"])
            request=self._obtain(ctx,request)

    def _repair_or_exhaust(self,ctx,operation,failed,prompt,payload,schema,role,allowed_actions,audit):
        all_repairs=self.store.repairs_for_operation(operation["operation_id"]); used=len([r for r in all_repairs if r.get("next_request_id")])
        limit=int(operation.get("output_repair_limit",0))
        existing=[r for r in all_repairs if r.get("failed_call_id")==failed["request_id"]]
        if existing:
            link=existing[-1]
            if link.get("next_request_id"):
                try: return self.store.model_call(link["next_request_id"])
                except Fault as exc:
                    if exc.code!="handoff_not_found": raise
                    messages=self.build_repair_messages(failed["base_messages"],failed.get("raw_response") or "",link["rendered_feedback"],link["feedback_envelope"])
                    return self._allocate_request(ctx,operation,failed["turn_index"],prompt,payload,schema,role,allowed_actions,purpose="output_repair",repair_ordinal=link["repair_ordinal"],request_id=link["next_request_id"],messages=messages,feedback=link["rendered_feedback"],repair_id=link["repair_id"])
            raise audit_fault(audit)
        signature=hashlib.sha256((failed.get("raw_response") or "").encode("utf-8")+canonical(audit.get("findings",[])).encode("utf-8")).hexdigest()
        repeats=sum(1 for r in all_repairs if r.get("failure_signature")==signature)
        rendered,envelope=repair_feedback(phase=audit["phase"],task_prompt=prompt,source_context=payload,output_schema=schema,allowed_actions=allowed_actions,failed_raw=failed.get("raw_response") or "",findings=audit["findings"],repeat_count=repeats)
        repair_id=uid()
        if used>=limit:
            link={"repair_id":repair_id,"kind":"output_repair","audit_phase":audit["phase"],"run_id":ctx.run["id"],"stage_id":ctx.node["id"],"node_id":ctx.node["id"],"stage_attempt":ctx.attempt,"attempt":ctx.attempt,"operation_id":operation["operation_id"],"failed_call_id":failed["request_id"],"failed_raw_response_ref":failed.get("response_path"),"audit_codes":[f.get("code") for f in audit["findings"]],"findings":deepcopy(audit["findings"]),"rendered_feedback":rendered,"feedback_envelope":envelope,"repair_ordinal":used,"frozen_limit":limit,"next_request_id":None,"next_call_id":None,"outcome":"exhausted","failure_signature":signature,"created":time.time()}
            self.store.put_repair(link); operation["status"]="exhausted"; operation["output_repairs_used"]=used; self.store.update_operation(operation["operation_id"],operation)
            ctx.run.setdefault("repair_counts",{})[ctx.node["id"]]=used; ctx.save(); raise audit_fault(audit)
        next_id=uid(); ordinal=used+1
        link={"repair_id":repair_id,"kind":"output_repair","audit_phase":audit["phase"],"run_id":ctx.run["id"],"stage_id":ctx.node["id"],"node_id":ctx.node["id"],"stage_attempt":ctx.attempt,"attempt":ctx.attempt,"operation_id":operation["operation_id"],"failed_call_id":failed["request_id"],"failed_raw_response_ref":failed.get("response_path"),"audit_codes":[f.get("code") for f in audit["findings"]],"findings":deepcopy(audit["findings"]),"rendered_feedback":rendered,"feedback_envelope":envelope,"repair_ordinal":ordinal,"frozen_limit":limit,"next_request_id":next_id,"next_call_id":None,"outcome":"pending","failure_signature":signature,"created":time.time()}
        # The repair reservation is represented first. If interrupted before request creation,
        # resume reconstructs the pending request deterministically from this persisted envelope.
        self.store.put_repair(link)
        messages=self.build_repair_messages(failed["base_messages"],failed.get("raw_response") or "",rendered,envelope)
        next_req=self._allocate_request(ctx,operation,failed["turn_index"],prompt,payload,schema,role,allowed_actions,purpose="output_repair",repair_ordinal=ordinal,request_id=next_id,messages=messages,feedback=rendered,repair_id=repair_id)
        link=self.store.repair(repair_id); link["outcome"]="in_progress"; self.store.update_repair(repair_id,link)
        operation["output_repairs_used"]=ordinal; self.store.update_operation(operation["operation_id"],operation)
        ctx.run.setdefault("repair_counts",{})[ctx.node["id"]]=ordinal; ctx.run["feedback"][ctx.node["id"]]={"kind":"output_repair","repair_id":repair_id,"phase":audit["phase"],"findings":deepcopy(audit["findings"]),"rendered_feedback":rendered}; ctx.save()
        ctx.event("output_repair",{"repair_id":repair_id,"failed_request_id":failed["request_id"],"next_request_id":next_id,"ordinal":ordinal,"limit":limit,"phase":audit["phase"]})
        return next_req

    def _self_physical(self,ctx,request):
        rows=self.store.physical_calls_for_request(request["request_id"])
        if rows: return rows[-1]
        call={"call_id":uid(),"request_id":request["request_id"],"run_id":request["run_id"],"node_id":request["node_id"],"attempt":request["attempt"],"operation_id":request["operation_id"],"purpose":request["purpose"],"transport_ordinal":0,"retry_kind":None,"executor":"self","model":request["effective_model"].get("model"),"status":"waiting_self","handoff":True,"dispatched":False,"created":time.time(),"response_metadata":None}
        call=self.store.put_physical_call(call)
        if request.get("repair_id"):
            link=self.store.repair(request["repair_id"]); link["next_call_id"]=call["call_id"]; self.store.update_repair(link["repair_id"],link)
        return call

    def _obtain(self,ctx,request):
        run=ctx.run; profile=run["profile"]; request_id=request["request_id"]
        if profile["executor"]=="self":
            call=self._self_physical(ctx,request); ctx.event("model_waiting",{"request_id":request_id,"call_id":call["call_id"],"role":request["role"],"purpose":request["purpose"]}); raise ModelSuspension(request_id)
        observed_destination=classify_destination(profile); request["observed_destination"]=observed_destination; self.store.update_model_call(request_id,request); ctx.event("destination_observed",{"request_id":request_id,**observed_destination})
        credential=self.profiles.credential(profile)
        try: provider=make_provider(profile,credential,run["snapshot"].get("recording"))
        except Fault as exc:
            request["status"]="setup_error"; request["error"]=present_error(exc,stage=request["node_id"],attempt=request["attempt"]); self.store.update_model_call(request_id,request); raise
        retries=int(run["snapshot"]["policy"].get("transport_retries",1)); prior=self.store.physical_calls_for_request(request_id)
        automatic_used=len([c for c in prior if c.get("retry_kind")=="transport"])
        manual=bool(request.pop("manual_retry",False)); retry_ordinal=max([int(c.get("transport_ordinal",-1)) for c in prior] or [-1])+1
        while True:
            # A pre-created but undispatched physical call is safe to reuse after restart.
            existing=[c for c in self.store.physical_calls_for_request(request_id) if c.get("status")=="pending" and not c.get("dispatched")]
            if existing: call=existing[-1]
            else:
                kind="manual" if manual else ("transport" if retry_ordinal>0 else None)
                call={"call_id":uid(),"request_id":request_id,"run_id":request["run_id"],"node_id":request["node_id"],"attempt":request["attempt"],"operation_id":request["operation_id"],"purpose":request["purpose"],"transport_ordinal":retry_ordinal,"retry_kind":kind,"executor":profile["executor"],"model":request["effective_model"].get("model"),"status":"pending","handoff":False,"dispatched":False,"created":time.time(),"response_metadata":None}
                call=self.store.put_physical_call(call)
            remaining=min(run["snapshot"]["policy"]["limits"]["seconds"]-run["usage"]["seconds"],run["snapshot"]["policy"]["limits"]["node_seconds"]-run["node_usage"][ctx.node["id"]]["seconds"])
            timeout=max(.01,min(float(request["effective_model"].get("timeout_seconds",60)),remaining))
            wire={k:deepcopy(request[k]) for k in ["contract_version","run_id","node_id","attempt","operation_id","call_index","role","messages","output_schema","allowed_actions"]}; wire.update(request_id=request_id,timeout=timeout)
            call["status"]="dispatching"; call["dispatched"]=True; call["started_at"]=time.time(); self.store.update_physical_call(call["call_id"],call)
            request["status"]="dispatching"; request.pop("error",None); self.store.update_model_call(request_id,request)
            ctx.event("model_request",{"request_id":request_id,"call_id":call["call_id"],"role":request["role"],"purpose":request["purpose"],"executor":profile["executor"],"model":request["effective_model"].get("model"),"reservation":request["reservation"],"transport_ordinal":retry_ordinal})
            try:
                offset=run["provider_offsets"].get(ctx.node["id"],0); run["provider_offsets"][ctx.node["id"]]=offset+1; ctx.save()
                response=provider.complete(wire,ctx.node["id"],offset); self._guard_secret(response["text"],credential)
                request["raw_response"]=response["text"]; request["response_metadata"]=_safe_usage_meta(response); request["status"]="submitted"; self.store.update_model_call(request_id,request)
                call["status"]="complete"; call["finished_at"]=time.time(); call["response_metadata"]=deepcopy(request["response_metadata"]); self.store.update_physical_call(call["call_id"],call)
                ctx.event("model_response",{"request_id":request_id,"call_id":call["call_id"],**request["response_metadata"]}); return request
            except Fault as exc:
                call["status"]="failed"; call["finished_at"]=time.time(); call["error"]=present_error(exc,stage=request["node_id"],attempt=request["attempt"]); self.store.update_physical_call(call["call_id"],call); ctx.event("model_error",{"request_id":request_id,"call_id":call["call_id"],"code":exc.code})
                transient=exc.code in {"provider_transient_failure","provider_timeout"}
                if transient and automatic_used<retries:
                    automatic_used+=1; retry_ordinal+=1; manual=False; ctx.reserve(turns=1,tokens=request["reservation"]); continue
                request["status"]="provider_error"; request["error"]=present_error(exc,stage=request["node_id"],attempt=request["attempt"]); self.store.update_model_call(request_id,request)
                if request.get("repair_id"):
                    link=self.store.repair(request["repair_id"]); link["outcome"]="provider_failure"; self.store.update_repair(link["repair_id"],link)
                raise

    def _guard_secret(self,text,extra=None):
        for secret in [*self.profiles.vault.known_values(),extra]:
            if secret and secret in text: raise Fault("secret_echo_detected")

    def guard_persist(self,value,profile):
        text=json.dumps(value,ensure_ascii=False,sort_keys=True); self._guard_secret(text,self.profiles.credential(profile))

    def handoff(self,run_id):
        run=self.store.run(run_id)
        if run.get("status")!="waiting_model" or not run.get("waiting_request_id"): raise Fault("handoff_not_pending")
        rec=self.store.model_call(run["waiting_request_id"]); attempt=self.store.step_attempt(run_id,rec["node_id"],rec["attempt"]) or {}
        keys=["contract_version","request_id","run_id","node_id","attempt","operation_id","turn_index","call_index","purpose","repair_ordinal","role","workflow_id","workflow_version","resolved_node_input_ref","messages","output_schema","allowed_actions","effective_model","evidence_scope","limits","repair_feedback"]
        out={k:deepcopy(rec.get(k)) for k in keys}; out["resolved_inputs"]=deepcopy(attempt.get("resolved_inputs",{})); return out

    def submit(self,run_id,envelope):
        if not isinstance(envelope,dict) or set(envelope)!={"contract_version","request_id","content"} or envelope.get("contract_version")!=STEP_CONTRACT_VERSION or not isinstance(envelope.get("content"),str): raise Fault("invalid_submission")
        run=self.store.run(run_id); rec=self.store.model_call(envelope["request_id"])
        if rec["run_id"]!=run_id: raise Fault("stale_handoff")
        answered=rec["status"] in {"submitted","complete","invalid"}; stale=run.get("status")=="cancelled" or run.get("attempts",{}).get(rec["node_id"],rec["attempt"])>rec["attempt"] or run.get("waiting_request_id")!=rec["request_id"]
        if answered:
            if stale: raise Fault("stale_handoff")
            if rec.get("raw_response")==envelope["content"]: return rec
            raise Fault("response_already_submitted")
        if run.get("status")!="waiting_model" or run.get("waiting_request_id")!=rec["request_id"] or rec["status"]!="pending": raise Fault("stale_handoff")
        self._guard_secret(envelope["content"]); rec["raw_response"]=envelope["content"]; rec["response_metadata"]={"executor":"self","returned_model":None,"usage":None,"finish_reason":None,"duration":None,"cost":None,"currency":None,"executor_label":run.get("self_executor_label","unreported")}; rec["status"]="submitted"; self.store.update_model_call(rec["request_id"],rec)
        rows=self.store.physical_calls_for_request(rec["request_id"])
        if rows:
            call=rows[-1]; call["status"]="complete"; call["finished_at"]=time.time(); call["response_metadata"]=deepcopy(rec["response_metadata"]); self.store.update_physical_call(call["call_id"],call)
        return rec
