"""Shared persisted logical model-call contract for every executor."""
from __future__ import annotations
from copy import deepcopy
import json,time,uuid
from .contracts import Fault, ModelSuspension, parse, validate
from .providers import make_provider
from .profiles import classify_destination
from . import STEP_CONTRACT_VERSION


def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


class ModelSteps:
    def __init__(self,store,profiles): self.store,self.profiles=store,profiles

    def build_messages(self,prompt,payload,schema):
        return [{"role":"system","content":prompt+"\nReturn JSON matching this schema:\n"+json.dumps(schema,ensure_ascii=False,sort_keys=True)},
                {"role":"user","content":json.dumps(payload,ensure_ascii=False,sort_keys=True)}]

    def parse_text(self,text,schema):
        value=parse(text); validate(value,schema); return value

    def call(self,ctx,prompt,payload,schema,role="reasoning",allowed_actions=None):
        ctx.call_index+=1; run=ctx.run; node=ctx.node; attempt=ctx.attempt
        profile=run["profile"]; settings=profile["effective_roles"].get(role)
        if settings is None: raise Fault("profile_role_missing",role)
        messages=self.build_messages(prompt,payload,schema)
        logical={"contract_version":STEP_CONTRACT_VERSION,"run_id":run["id"],"node_id":node["id"],"attempt":attempt,"call_index":ctx.call_index,
                 "role":role,"workflow_id":run["snapshot"]["manifest"]["id"],"workflow_version":run["snapshot"]["workflow"].get("version"),
                 "messages":messages,"output_schema":schema,"allowed_actions":allowed_actions or [],"effective_model":deepcopy(settings),
                 "evidence_scope":{"corpus_ids":list(run["corpora"])},"limits":deepcopy(run["snapshot"]["policy"]["limits"])}
        can=canonical(logical); existing=self.store.model_call_at(run["id"],node["id"],attempt,ctx.call_index)
        reservation=len(json.dumps(messages,ensure_ascii=False).encode("utf-8"))+int(settings.get("max_tokens",4096))
        resend=False
        if existing:
            if existing.get("canonical")!=can: raise Fault("resume_contract_mismatch")
            if existing["status"]=="complete":
                ctx.event("response_replayed",{"request_id":existing["request_id"]}); return existing["parsed"]
            if existing["status"] in {"submitted","invalid"}: return self._consume(existing)
            if existing["status"]=="pending" and profile["executor"]=="self": raise ModelSuspension(existing["request_id"])
            if existing["status"]=="dispatching":
                existing["status"]="dispatch_unknown"; self.store.update_model_call(existing["request_id"],existing)
                raise Fault("external_response_unknown")
            if existing["status"]=="dispatch_unknown":
                if not run.pop("external_retry_authorized",False): raise Fault("external_response_unknown")
                existing["status"]="pending"; resend=True
                ctx.event("external_response_retry",{"request_id":existing["request_id"],"provider_may_bill_twice":True})
            elif existing["status"]=="error":
                if not run.pop("provider_retry_authorized",False):
                    err=existing.get("error",{}); raise Fault(err.get("code","provider_request_rejected"),err.get("detail",""))
                existing["status"]="pending"; resend=True
            record=existing; request_id=record["request_id"]
        else:
            request_id=uuid.uuid4().hex
            record={**logical,"request_id":request_id,"resolved_node_input_ref":f"{run['id']}:{node['id']}:{attempt}","status":"pending","canonical":can,
                    "raw_response":None,"parsed":None,"findings":[],"response_metadata":None,"reservation":reservation}
            record=self.store.put_model_call(record); ctx.reserve(turns=1,tokens=reservation)
        if resend: ctx.reserve(turns=1,tokens=reservation)
        if profile["executor"]=="self":
            ctx.event("model_waiting",{"request_id":request_id,"role":role}); raise ModelSuspension(request_id)
        observed_destination=classify_destination(profile)
        record["observed_destination"]=observed_destination; self.store.update_model_call(request_id,record)
        ctx.event("destination_observed",{"request_id":request_id,**observed_destination})
        credential=self.profiles.credential(profile)
        try:
            provider=make_provider(profile,credential,run["snapshot"].get("recording"))
        except Fault as exc:
            record["status"]="error"; record["error"]={"code":exc.code,"detail":exc.detail}; self.store.update_model_call(request_id,record); raise
        retries=run["snapshot"]["policy"].get("transport_retries",1); last=None
        for retry in range(retries+1):
            if retry: ctx.reserve(turns=1,tokens=reservation)
            remaining=min(run["snapshot"]["policy"]["limits"]["seconds"]-run["usage"]["seconds"],run["snapshot"]["policy"]["limits"]["node_seconds"]-run["node_usage"][node["id"]]["seconds"])
            timeout=max(.01,min(float(settings.get("timeout_seconds",60)),remaining))
            wire={**logical,"request_id":request_id,"timeout":timeout}
            record["status"]="dispatching"; record.pop("error",None); self.store.update_model_call(request_id,record)
            ctx.event("model_request",{"request_id":request_id,"role":role,"executor":profile["executor"],"model":settings.get("model"),"reservation":reservation})
            try:
                offset=run["provider_offsets"].get(node["id"],0); run["provider_offsets"][node["id"]]=offset+1; ctx.save()
                response=provider.complete(wire,node["id"],offset)
                self._guard_secret(response["text"],credential)
                record["raw_response"]=response["text"]; record["response_metadata"]={k:v for k,v in response.items() if k!="text"}; record["status"]="submitted"
                self.store.update_model_call(request_id,record); ctx.event("model_response",{"request_id":request_id,**record["response_metadata"]})
                return self._consume(record)
            except Fault as exc:
                last=exc; ctx.event("model_error",{"request_id":request_id,"code":exc.code})
                transient=exc.code in {"provider_transient_failure","provider_timeout"}
                if not transient or retry>=retries:
                    record["status"]="error"; record["error"]={"code":exc.code,"detail":exc.detail}; self.store.update_model_call(request_id,record); raise
        raise last

    def _guard_secret(self,text,extra=None):
        for secret in [*self.profiles.vault.known_values(),extra]:
            if secret and secret in text: raise Fault("secret_echo_detected")

    def guard_persist(self,value,profile):
        text=json.dumps(value,ensure_ascii=False,sort_keys=True)
        self._guard_secret(text,self.profiles.credential(profile))

    def _consume(self,record):
        if record.get("raw_response") is None: raise Fault("provider_incompatible_response")
        try:
            value=self.parse_text(record["raw_response"],record["output_schema"])
        except Fault as exc:
            record["status"]="invalid"; record["findings"]=exc.findings or [{"code":exc.code}]; self.store.update_model_call(record["request_id"],record); raise
        record["parsed"]=value; record["status"]="complete"; record["findings"]=[]; self.store.update_model_call(record["request_id"],record)
        return value

    def handoff(self,run_id):
        run=self.store.run(run_id)
        if run.get("status")!="waiting_model" or not run.get("waiting_request_id"): raise Fault("handoff_not_pending")
        rec=self.store.model_call(run["waiting_request_id"]); attempt=self.store.step_attempt(run_id,rec["node_id"],rec["attempt"]) or {}
        out={k:deepcopy(rec[k]) for k in ["contract_version","request_id","run_id","node_id","attempt","call_index","role","workflow_id","workflow_version","resolved_node_input_ref","messages","output_schema","allowed_actions","effective_model","evidence_scope","limits"]}
        out["resolved_inputs"]=deepcopy(attempt.get("resolved_inputs",{})); return out

    def submit(self,run_id,envelope):
        if not isinstance(envelope,dict) or set(envelope)!={"contract_version","request_id","content"} or envelope.get("contract_version")!=STEP_CONTRACT_VERSION or not isinstance(envelope.get("content"),str): raise Fault("invalid_submission")
        run=self.store.run(run_id); rec=self.store.model_call(envelope["request_id"])
        if rec["run_id"]!=run_id: raise Fault("stale_handoff")
        answered=rec["status"] in {"submitted","complete","invalid"}
        stale=run.get("status")=="cancelled" or run.get("attempts",{}).get(rec["node_id"],rec["attempt"])>rec["attempt"] or (run.get("nodes",{}).get(rec["node_id"])=="pending" and run.get("waiting_request_id")!=rec["request_id"])
        if answered:
            if stale: raise Fault("stale_handoff")
            if rec.get("raw_response")==envelope["content"]: return rec
            raise Fault("response_already_submitted")
        if run.get("status")!="waiting_model" or run.get("waiting_request_id")!=rec["request_id"] or rec["status"]!="pending": raise Fault("stale_handoff")
        self._guard_secret(envelope["content"]); rec["raw_response"]=envelope["content"]; rec["response_metadata"]={"executor":"self","returned_model":None,"usage":None,"finish_reason":None,"duration":None,"executor_label":run.get("self_executor_label","unreported")}; rec["status"]="submitted"
        self.store.update_model_call(rec["request_id"],rec); return rec
