"""Shared service used by CLI and local browser."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json,yaml,uuid,subprocess
from . import __version__
from .compiler import asset,load_application
from .contracts import Fault,validate
from .storage import Store
from .modules import registry
from .model_steps import ModelSteps
from .profiles import ProfileRegistry,CredentialVault
from .runtime import Runner,aggregate_origins
from .guidelines import GuidelineRegistry
from .fixtures import FixtureManager
from .paths import RuntimePaths, default_execution_config
from .usage import aggregate_usage
from .errors import present_error
from .audit import audit_json,audit_fault,repair_feedback

def _source_version(root):
    try:
        commit=subprocess.run(["git","-C",str(Path(root).resolve()),"rev-parse","HEAD"],capture_output=True,text=True,timeout=2,check=True).stdout.strip()
        dirty=bool(subprocess.run(["git","-C",str(Path(root).resolve()),"status","--porcelain"],capture_output=True,text=True,timeout=2,check=True).stdout.strip())
        return {"commit":commit,"dirty":dirty}
    except (OSError,subprocess.SubprocessError):
        return {"commit":None,"dirty":None}

class Service:
    def __init__(self,apps="applications",data=None,profiles="model_profiles",guidelines="guideline_sets",fixtures="tests/fixtures/steps",writer=True,runs_root=None,config_root=None,launch_root=None):
        self.apps=Path(apps).resolve(); explicit_state=data is not None
        launch=Path(launch_root or self.apps.parent).resolve()
        fixture_repo=Path(fixtures).resolve(); fixture_root=fixture_repo.parent if fixture_repo.name=="steps" else fixture_repo
        effective_runs=runs_root
        if explicit_state and effective_runs is None: effective_runs=Path(data).resolve()/"runs"
        self.paths=RuntimePaths.resolve(launch_root=launch,state_root=data,runs_root=effective_runs,config_root=config_root,fixtures_root=fixture_root).ensure_runtime_dirs(writer=writer,explicit_state=explicit_state)
        self.execution_defaults=default_execution_config(self.paths.config_root)
        self.store=Store(self.paths.state_root,writer=writer,runs_root=self.paths.runs_root); self.registry=registry(); self.vault=CredentialVault(); self.profiles=ProfileRegistry(profiles,self.store,self.vault)
        self.model_steps=ModelSteps(self.store,self.profiles); self.runner=Runner(self.store,self.registry,self.model_steps); self.guidelines=GuidelineRegistry(guidelines,self.store)
        self.fixtures=FixtureManager(self.store,fixtures,self.paths.scratch_root)
    def close(self): self.store.close()
    def applications(self):
        rows=[]
        for p in sorted(self.apps.glob("*/application.yaml")):
            doc=yaml.safe_load(p.read_text(encoding="utf-8")); doc["version"]=__version__; rows.append(doc)
            if doc.get("purposes"):
                configured=self.load(doc["id"])["purposes"]
                doc["purposes"]={"default":configured["default"],"options":[{"id":x["id"],"label":x["label"]} for x in configured["options"]]}
        return rows
    def root(self,app):
        matches=[p.parent for p in self.apps.glob("*/application.yaml") if yaml.safe_load(p.read_text(encoding="utf-8"))["id"]==app]
        if len(matches)!=1: raise Fault("application_not_found")
        return matches[0]
    def load(self,app):
        root=self.root(app); snap=load_application(root,self.registry); snap["input_schema"]=json.loads(asset(root,snap["manifest"]["input_schema"]).read_text(encoding="utf-8")); snap["source_version"]=_source_version(self.apps.parent); snap["execution_defaults"]=deepcopy(self.execution_defaults); return snap
    def example(self,app,name):
        if not name or "/" in name or "\\" in name or ".." in name: raise Fault("example_name")
        return json.loads(asset(self.root(app),f"examples/{name}.json").read_text(encoding="utf-8"))
    def configure_profile(self,profile_id,overlay,credential=None,clear_credential=False): return self.profiles.configure(profile_id,overlay,credential,clear_credential)
    def profile_list(self,workflow_id=None): return self.profiles.list(workflow_id)
    def _adapt_input(self,workflow,input_mode,data,example=None):
        if input_mode=="demo":
            fixture=self.example(workflow,example or "standard"); return fixture["input"],fixture.get("responses",{}),{"task_input":"synthetic","evidence":{"demo_input":"synthetic"},"revision_feedback":{}}
        if input_mode=="free_text":
            if workflow=="clinical_letter":
                notes=data.get("notes"); purpose=data.get("purpose")
                if not isinstance(notes,str) or not notes.strip() or not isinstance(purpose,str) or not purpose.strip(): raise Fault("invalid_input")
                if len(notes)>30000: raise Fault("input_too_large")
                value={"purpose":purpose,"sources":[{"id":"clinical_note","format":"text","title":"Clinical notes","content":notes}],"required_fact_ids":[]}
                return value,None,{"task_input":"user_supplied","evidence":{"clinical_input":"user_supplied"},"revision_feedback":{}}
            question=data.get("question")
            if not isinstance(question,str) or not question.strip(): raise Fault("invalid_input")
            if len(question)>8000: raise Fault("input_too_large")
            return {"question":question,"context":{},"filters":[]},None,{"task_input":"user_supplied","evidence":{},"revision_feedback":{}}
        if input_mode in {"advanced","copied"}:
            return deepcopy(data),None,{"task_input":"user_supplied" if input_mode=="advanced" else "unknown","evidence":{},"revision_feedback":{}}
        raise Fault("input_mode_invalid")
    def start(self,workflow_id,profile_id,input_mode="free_text",input_data=None,profile_overrides=None,guideline_selection=None,example=None,developer=False,derived_from_run_id=None,origin_override=None,retry_overrides=None):
        snap=self.load(workflow_id); profile=self.profiles.resolve(profile_id,profile_overrides or {},workflow_id=workflow_id,runnable=True)
        if retry_overrides and not developer: raise Fault("developer_disabled")
        self.store.set_preference("selected_profile:"+workflow_id,profile_id)
        if profile["executor"]=="self" and not developer: raise Fault("developer_disabled")
        value,recording,origins=self._adapt_input(workflow_id,input_mode,input_data or {},example)
        if snap.get("purposes"):
            selected=next((x for x in snap["purposes"]["options"] if x["id"]==value.get("purpose")),None)
            if selected is None: raise Fault("invalid_purpose")
            snap["selected_purpose"]=deepcopy(selected)
        if origin_override: origins=deepcopy(origin_override)
        validate(value,snap["input_schema"]); corpora=[]
        if recording is not None:
            if profile["executor"]!="recorded": raise Fault("recorded_profile_required")
            snap["recording"]=recording
        elif profile["executor"]=="recorded": raise Fault("recorded_input_mismatch")
        if workflow_id=="guideline_qa":
            sel=guideline_selection or {"set_id":"demo","selector":"default"}; resolved=self.guidelines.resolve(sel["set_id"],sel.get("selector","default"),developer=developer); corpora=[resolved["corpus_id"]]; snap["guideline_selection"]=resolved; origins["evidence"][resolved["corpus_id"]]=resolved["content_origin"]
        return self.runner.create(snap,value,profile,corpora,origins=origins,derived_from_run_id=derived_from_run_id,retry_overrides=retry_overrides)

    def _finalize_guideline_import(self,rid):
        run=self.store.run(rid); meta=run.get("guideline_import")
        if not meta or run.get("status")!="completed" or meta.get("snapshot_id"): return run
        artifact=self.store.artifact(rid,"import")["payload"]
        cid=self.guidelines.publish_devel(meta["set_id"],meta["sources"],meta["profile"],artifact["items"],{"mode":"model","created":run.get("created"),"producing_run":rid})
        run=self.store.run(rid); run["guideline_import"]["snapshot_id"]=cid; self.store.put_run(run); self.store.event(rid,"guideline_devel_published",{"set_id":meta["set_id"],"snapshot_id":cid}); return run
    def import_guideline_devel(self,set_id,sources,profile=None,profile_id=None,developer=False):
        if not developer: raise Fault("developer_disabled")
        ingestion=deepcopy(profile or self.guidelines.devel_profile(set_id))
        if ingestion.get("mode")!="model":
            return {"status":"completed","snapshot_id":self.guidelines.import_devel(set_id,sources,ingestion),"run_id":None}
        if not profile_id: raise Fault("model_profile_required")
        model_profile=self.profiles.resolve(profile_id,{},workflow_id="guideline_qa",runnable=True)
        app=self.load("guideline_qa")
        input_schema={"type":"object","required":["sources"],"additionalProperties":False,"properties":{"sources":{"type":"array","minItems":1,"items":{"type":"object"}}}}
        node_input_schema={"type":"object","required":["data"],"additionalProperties":False,"properties":{"data":deepcopy(input_schema)}}
        output_schema={"type":"object","required":["corpus_id","items"],"additionalProperties":False,"properties":{"corpus_id":{"type":"string"},"items":{"type":"array","items":{"type":"object"}}}}
        node={"id":"import","module":"ingest","model_dependent":True,"inputs":{"data":"run.input"},"input_schema":node_input_schema,"config":{"profile":ingestion,"scope":"run","name":"guideline_import"},"schema":output_schema}
        snap={"manifest":{"id":"guideline_qa","name":"Guideline development import","version":__version__},"workflow":{"version":2,"nodes":[node],"output":"import"},"policy":deepcopy(app["policy"]),"input_schema":input_schema,"source_version":deepcopy(app.get("source_version"))}
        snap["policy"]["required_checks"]=[]; snap["policy"]["human_approval"]=False; snap["execution_defaults"]=deepcopy(self.execution_defaults)
        rid=self.runner.create(snap,{"sources":deepcopy(sources)},model_profile,origins={"task_input":"user_supplied","evidence":{},"revision_feedback":{}})
        run=self.store.run(rid); run["guideline_import"]={"set_id":set_id,"sources":deepcopy(sources),"profile":ingestion,"snapshot_id":None}; self.store.put_run(run)
        run=self.runner.advance(rid)
        if run["status"]=="completed": run=self._finalize_guideline_import(rid)
        return {"status":run["status"],"run_id":rid,"snapshot_id":run.get("guideline_import",{}).get("snapshot_id")}

    def legacy_copy_payload(self,rid):
        run=self.store.run(rid)
        if "run_contract_version" in run and not run.get("legacy"): raise Fault("legacy_run_required")
        workflow=run.get("snapshot",{}).get("manifest",{}).get("id")
        if workflow not in {"clinical_letter","guideline_qa"}: raise Fault("legacy_workflow_unsupported")
        try:
            snap=self.load(workflow); validate(run.get("input"),snap["input_schema"])
            if snap.get("purposes") and run.get("input",{}).get("purpose") not in {x["id"] for x in snap["purposes"]["options"]}: raise Fault("legacy_input_conversion_required")
        except Fault as exc:
            if exc.code in {"application_not_found"}: raise Fault("legacy_workflow_unsupported")
            raise Fault("legacy_input_conversion_required",findings=exc.findings) from None
        return {"workflow_id":workflow,"input":deepcopy(run.get("input")),"derived_from_run_id":rid,"origins":deepcopy(run.get("origins") or {"task_input":"unknown","evidence":{},"revision_feedback":{}}),"old_metadata":{"status":run.get("status")}}
    def inspect(self,rid,developer=False):
        result=self.store.inspection_snapshot(rid); run=result["run"]
        result["usage"]=aggregate_usage(result["model_calls"],result["physical_calls"],result["semantic_revisions"])
        result["paths"]={"run_folder":str(self.store.run_dir(rid).resolve()),"state_root":str(self.paths.state_root),"runs_root":str(self.paths.runs_root)}
        stored=run.get("error")
        if isinstance(stored,dict) and stored:
            if stored.get("explanation") and stored.get("remedy"):
                result["current_error"]=deepcopy(stored)
            else:
                result["current_error"]=present_error(stored,fallback=True)
                result["current_error"]["presentation_source"]="current_fallback_for_legacy_error"
        else:
            result["current_error"]=None
        if run.get("waiting_request_id") and developer: result["handoff"]=self.model_steps.handoff(rid)
        return result
    def resume(self,rid):
        run=self.runner.resume(rid)
        return self._finalize_guideline_import(rid) if run.get("status")=="completed" and run.get("guideline_import") else run
    def self_handoff(self,rid): return self.model_steps.handoff(rid)
    def self_submit(self,rid,envelope):
        self.model_steps.submit(rid,envelope); run=self.runner.advance(rid)
        return self._finalize_guideline_import(rid) if run.get("status")=="completed" and run.get("guideline_import") else run
    def review(self,rid,payload): return self.runner.review(rid,payload)
    def delete(self,rid): self.store.delete_run(rid)
    def steps(self,workflow_id):
        snap=self.load(workflow_id); return [{"id":n["id"],"module":n["module"],"purpose":n.get("config",{}).get("purpose") or f"Execute the {n["id"]} model stage.","input_schema":n.get("input_schema",{}),"output_schema":n.get("schema",{}),"role":n.get("config",{}).get("role")} for n in snap["workflow"]["nodes"] if n.get("model_dependent")]
    def capture_fixture(self,rid,node,attempt): return self.fixtures.capture(self.store.run(rid),node,attempt)
    def _validate_fixture_for_step(self,doc):
        self.fixtures.validate_envelope(doc); snap=self.load(doc["workflow_id"]); nodes={n["id"]:n for n in snap["workflow"]["nodes"]}
        node=nodes.get(doc["node_id"]);
        if not node or not node.get("model_dependent"): raise Fault("step_not_model_dependent")
        validate(doc["resolved_inputs"],node.get("input_schema",{})); return node
    def save_scratch_fixture(self,doc):
        self._validate_fixture_for_step(doc); return str(self.fixtures.save_scratch(doc))
    def list_fixtures(self): return self.fixtures.list()
    def delete_scratch_fixture(self,fixture_id,version): return self.fixtures.delete_scratch(fixture_id,version)
    def promote_fixture(self,fixture_id,version,new_id,new_version,suitability,acknowledge,actor):
        doc=self.fixtures.registered(fixture_id,version); self._validate_fixture_for_step(doc)
        return str(self.fixtures.promote(doc,new_id,new_version,suitability,acknowledge,actor,doc["workflow_id"],doc["node_id"]))
    def run_step(self,workflow_id,node_id,fixture_doc,profile_id,profile_overrides=None,tape=None,configuration_source="current",developer=False,retry_overrides=None):
        if not developer: raise Fault("developer_disabled")
        snap=self.load(workflow_id); nodes={n["id"]:n for n in snap["workflow"]["nodes"]}
        if fixture_doc.get("workflow_id")!=workflow_id or fixture_doc.get("node_id")!=node_id: raise Fault("fixture_contract_invalid")
        validated=self._validate_fixture_for_step(fixture_doc); node=deepcopy(validated); resolved=deepcopy(fixture_doc["resolved_inputs"])
        if configuration_source not in {"current","captured"}: raise Fault("fixture_configuration_invalid")
        if configuration_source=="captured" and not fixture_doc.get("captured_configuration"): raise Fault("fixture_configuration_missing")
        # Isolated execution keeps the selected node's model/check behaviour but not
        # full-workflow control transitions.  Ancestor-dependent conditions and
        # semantic-revision targets cannot be meaningful when the tester deliberately
        # supplies the already-resolved input and runs no ancestors/downstream nodes.
        node["needs"]=[]; node.pop("review",None); node.pop("when",None)
        node["inputs"]={k:{"from":f"run.input.{k}","optional":v is None} for k,v in resolved.items()}; one={"manifest":deepcopy(snap["manifest"]),"workflow":{"version":2,"nodes":[node],"output":node_id},"policy":deepcopy(snap["policy"]),"input_schema":node.get("input_schema",{})}; one["policy"]["human_approval"]=False; one["policy"]["required_checks"]=[]
        if configuration_source=="captured": node["config"]=deepcopy(fixture_doc["captured_configuration"])
        profile=self.profiles.resolve(profile_id,profile_overrides or {},workflow_id=workflow_id,runnable=True); corpora=[]
        # Materialise fixture evidence under fresh temporary corpus IDs. Rewrite only
        # structured corpus/revision fields whose value exactly matches a captured ID;
        # never replace arbitrary clinical strings.
        from .storage import uid
        mapping={c["corpus_id"]:uid() for c in fixture_doc.get("evidence_snapshots",[])}
        def remap(value):
            if isinstance(value,dict):
                return {k:(mapping[v] if k in {"corpus_id","revision"} and isinstance(v,str) and v in mapping else remap(v)) for k,v in value.items()}
            if isinstance(value,list): return [remap(v) for v in value]
            return value
        resolved=remap(resolved)
        for c in fixture_doc.get("evidence_snapshots",[]):
            new_id=mapping[c["corpus_id"]]
            corpora.append(self.store.publish("fixture",fixture_doc["id"],c["profile"],c["sources"],remap(c["items"]),activate=False,cid=new_id))
        fixture_origins=deepcopy(fixture_doc.get("origins",{"task_input":"unknown","evidence":{},"revision_feedback":{}}))
        if isinstance(fixture_origins.get("evidence"),dict): fixture_origins["evidence"]={mapping.get(k,k):v for k,v in fixture_origins["evidence"].items()}
        if profile["executor"]=="recorded":
            if tape is None: raise Fault("recording_selection_required")
            self.fixtures.assert_registered(fixture_doc); self.fixtures.validate_tape(tape,fixture_doc)
            # Repository/scratch tapes are paired to the immutable fixture and therefore
            # may contain the fixture's captured corpus identifiers.  Isolated runs use
            # fresh temporary corpus IDs, so remap only structured JSON identity fields
            # in otherwise valid recorded responses.  Malformed/raw negative fixtures
            # remain byte-for-byte unchanged and exercise the normal syntax-repair path.
            def remap_tape_content(raw):
                try:
                    parsed=json.loads(raw)
                except (TypeError,ValueError):
                    return raw
                return json.dumps(remap(parsed),ensure_ascii=False,separators=(",",":"))
            rows=tape["responses"]; one["recording"]={node_id:{f"{r['attempt']}:{r['call_index']}":remap_tape_content(r["content"]) for r in rows}}
        one["execution_defaults"]=deepcopy(self.execution_defaults); rid=self.runner.create(one,resolved,profile,corpora,origins=fixture_origins,retry_overrides=retry_overrides); run=self.store.run(rid); run["isolated_step"]=True; run["configuration_source"]=configuration_source; run["fixture_identity"]={"id":fixture_doc["id"],"version":fixture_doc["version"]}; run["selected_tape"]={"id":tape["id"],"version":tape["version"]} if tape else None; self.store.put_run(run); return rid
    def verify_provider(self,profile_id,workflow_id,profile_overrides=None):
        profile=self.profiles.resolve(profile_id,profile_overrides or {},workflow_id=workflow_id,runnable=True)
        if profile["executor"] not in {"openrouter","lmstudio"}: raise Fault("verification_not_http")
        used={n.get("config",{}).get("role") for n in self.load(workflow_id)["workflow"]["nodes"] if n.get("model_dependent")}; used.discard(None); combinations={json.dumps(profile["effective_roles"][role],sort_keys=True):role for role in used}; results=[]
        from .providers import make_provider
        schema={"type":"object","required":["action","result"],"additionalProperties":False,"properties":{"action":{"const":"submit"},"result":{"type":"object","required":["ok"],"additionalProperties":False,"properties":{"ok":{"const":True}}}}}
        prompt="Return only the requested structured protocol-verification response."; payload={"task":"synthetic protocol verification","expected":{"action":"submit","result":{"ok":True}}}
        try: provider=make_provider(profile,self.profiles.credential(profile))
        except Fault as exc:
            results=[{"role":role,"success":False,"attempts":0,"repairs_used":0,"error":present_error(exc)} for role in combinations.values()]
            return {"profile_id":profile_id,"destination":profile["destination"],"results":results,"success":False}
        for _,role in combinations.items():
            settings=profile["effective_roles"][role]; base=self.model_steps.build_messages(prompt,payload,schema); messages=base
            attempts=0; repairs_used=0; failures=[]
            while True:
                req={"request_id":uuid.uuid4().hex,"role":role,"messages":messages,"timeout":settings.get("timeout_seconds",60),"output_schema":schema}
                attempts+=1
                try: response=provider.complete(req,"verify",repairs_used)
                except Fault as exc:
                    results.append({"role":role,"success":False,"attempts":attempts,"repairs_used":repairs_used,"error":present_error(exc)})
                    break
                raw=response["text"]; _,audit=audit_json(raw,schema)
                if audit["valid"]:
                    results.append({"role":role,"success":True,"attempts":attempts,"repairs_used":repairs_used,"elapsed":response.get("duration"),"returned_model":response.get("returned_model")})
                    break
                if audit["phase"] not in {"syntax","schema"} or repairs_used>=self.execution_defaults["output_repair_retries"]:
                    results.append({"role":role,"success":False,"attempts":attempts,"repairs_used":repairs_used,"error":present_error(audit_fault(audit))})
                    break
                repeats=sum(1 for failed_raw,findings in failures if failed_raw==raw and findings==audit["findings"])
                rendered,envelope=repair_feedback(phase=audit["phase"],task_prompt=prompt,source_context=payload,output_schema=schema,allowed_actions=None,failed_raw=raw,findings=audit["findings"],repeat_count=repeats)
                failures.append((raw,audit["findings"]))
                messages=self.model_steps.build_repair_messages(base,raw,rendered,envelope)
                repairs_used+=1
        return {"profile_id":profile_id,"destination":profile["destination"],"results":results,"success":all(x["success"] for x in results)}
