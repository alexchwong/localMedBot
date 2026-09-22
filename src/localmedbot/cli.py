from __future__ import annotations
import argparse,getpass,json,sys,uuid,yaml
from pathlib import Path
from . import __version__
from .contracts import Fault
from .service import Service
from .profiles import classify_destination
from .errors import present_error
from .paths import RuntimePaths
from .relocation import relocate_legacy


def _json_file(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def _import_legacy_profile(service,workflow,path):
    cfg=yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg,dict) or cfg.get("provider")!="http" or not cfg.get("base_url") or not cfg.get("model"):
        raise Fault("legacy_config_ambiguous")
    host=__import__("urllib.parse",fromlist=["urlsplit"]).urlsplit(cfg["base_url"]).hostname or ""
    if host=="openrouter.ai": executor="openrouter"
    else:
        probe={"executor":"lmstudio","base_url":cfg["base_url"]}
        if classify_destination(probe)["classification"]!="local_network": raise Fault("legacy_config_ambiguous")
        executor="lmstudio"
    pid=f"{workflow}.{executor}.default"; template=service.profiles.template(pid)
    if cfg.get("api_key_env") and cfg.get("api_key_env")!=template.get("credential_env"): raise Fault("legacy_config_ambiguous")
    overlay={"base_url":cfg["base_url"],"model":cfg["model"],"settings":{}}
    mapping={"temperature":"temperature","max_tokens":"max_tokens","timeout":"timeout_seconds","json_mode":"json_mode","reasoning":"reasoning"}
    for old,new in mapping.items():
        if old in cfg: overlay["settings"][new]=cfg[old]
    roles={}
    for role,row in (cfg.get("roles") or {}).items():
        if not isinstance(row,dict): raise Fault("legacy_config_ambiguous")
        roles[role]={}
        for old,new in mapping.items():
            if old in row: roles[role][new]=row[old]
        if "model" in row: roles[role]["model"]=row["model"]
    if roles: overlay["roles"]=roles
    service.configure_profile(pid,overlay)
    return {"profile_id":pid,"overlay":overlay}
def main(argv=None):
    p=argparse.ArgumentParser(prog="localmedbot"); p.add_argument("--apps",default="applications"); p.add_argument("--data",default=None,help="shared state root (legacy alias retained; default: ./state)"); p.add_argument("--runs-root",default=None,help="per-run storage root (default: ./runs)"); p.add_argument("--config-root",default=None,help="execution config root (default: ./config)"); p.add_argument("--profiles",default="model_profiles"); p.add_argument("--guidelines",default="guideline_sets"); p.add_argument("--fixtures",default="tests/fixtures/steps"); p.add_argument("--developer",action="store_true")
    sub=p.add_subparsers(dest="command",required=True); sub.add_parser("apps"); sub.add_parser("check")
    pp=sub.add_parser("profiles"); pps=pp.add_subparsers(dest="profiles_command",required=True); q=pps.add_parser("list"); q.add_argument("--workflow"); q=pps.add_parser("import-legacy"); q.add_argument("--workflow",required=True); q.add_argument("--file",required=True)
    pv=sub.add_parser("providers"); pvs=pv.add_subparsers(dest="providers_command",required=True); q=pvs.add_parser("verify"); q.add_argument("--profile",required=True); q.add_argument("--workflow",required=True)
    r=sub.add_parser("run"); r.add_argument("workflow"); r.add_argument("--profile",required=True); r.add_argument("--example"); r.add_argument("--input"); r.add_argument("--input-mode",choices=["demo","free_text","advanced"],default="advanced"); r.add_argument("--guideline-set",default="demo"); r.add_argument("--guideline-version",default="default"); r.add_argument("--output-repair-retries",type=int); r.add_argument("--semantic-revision-retries",type=int)
    for cmd in ["status","resume","cancel","delete"]: q=sub.add_parser(cmd); q.add_argument("run_id")
    rv=sub.add_parser("review"); rv.add_argument("run_id"); rv.add_argument("--revision",type=int); rv.add_argument("--actor",required=True); rv.add_argument("--decision",choices=["approve","reject","revise"],required=True); rv.add_argument("--comments",default=""); rv.add_argument("--target"); rv.add_argument("--review-request-id"); rv.add_argument("--ack-omission",action="append",default=[]); rv.add_argument("--ack-conflict",action="append",default=[])
    gl=sub.add_parser("guidelines"); gls=gl.add_subparsers(dest="guidelines_command",required=True); gls.add_parser("list"); qi=gls.add_parser("import"); qi.add_argument("set_id"); qi.add_argument("--sources",required=True); qi.add_argument("--profile"); qi.add_argument("--ingestion-profile"); qp=gls.add_parser("promote"); qp.add_argument("set_id"); qp.add_argument("--expected-snapshot",required=True); qp.add_argument("--note",required=True); qp.add_argument("--actor",required=True)
    st=sub.add_parser("steps"); sts=st.add_subparsers(dest="steps_command",required=True); q=sts.add_parser("list"); q.add_argument("workflow"); q=sts.add_parser("run"); q.add_argument("workflow"); q.add_argument("node"); q.add_argument("--fixture",required=True); q.add_argument("--profile",required=True); q.add_argument("--tape"); q.add_argument("--output-repair-retries",type=int); q.add_argument("--semantic-revision-retries",type=int)
    fx=sub.add_parser("fixtures"); fxs=fx.add_subparsers(dest="fixtures_command",required=True); q=fxs.add_parser("capture"); q.add_argument("run_id"); q.add_argument("--node",required=True); q.add_argument("--attempt",type=int,required=True); q.add_argument("--output",required=True); q=fxs.add_parser("promote"); q.add_argument("file"); q.add_argument("--id",required=True); q.add_argument("--version",type=int,required=True); q.add_argument("--suitability",required=True); q.add_argument("--acknowledge-reviewed",action="store_true"); q.add_argument("--actor",required=True)
    sf=sub.add_parser("self"); sfs=sf.add_subparsers(dest="self_command",required=True); q=sfs.add_parser("export"); q.add_argument("run_id"); q.add_argument("--output",required=True); q=sfs.add_parser("submit"); q.add_argument("run_id"); q.add_argument("--response",required=True)
    rl=sub.add_parser("relocate"); rl.add_argument("--source",default=".localmedbot")
    sv=sub.add_parser("serve"); sv.add_argument("--port",type=int,default=8765)
    a=p.parse_args(argv); service=None
    try:
        if a.command=="relocate":
            launch=Path(a.apps).resolve().parent; paths=RuntimePaths.resolve(launch_root=launch,state_root=a.data,runs_root=a.runs_root,config_root=a.config_root,fixtures_root=Path(a.fixtures).resolve().parent)
            result=relocate_legacy(a.source,paths.state_root,paths.runs_root,paths.scratch_root)
            print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
        read_only = a.command in {"apps","status","check"} or (a.command=="profiles" and a.profiles_command=="list") or (a.command=="guidelines" and a.guidelines_command=="list")
        launch=Path(a.apps).resolve().parent
        resolved_paths=RuntimePaths.resolve(launch_root=launch,state_root=a.data,runs_root=a.runs_root,config_root=a.config_root,fixtures_root=Path(a.fixtures).resolve().parent)
        service=Service(a.apps,resolved_paths.state_root,a.profiles,a.guidelines,a.fixtures,writer=not read_only,runs_root=resolved_paths.runs_root,config_root=resolved_paths.config_root,launch_root=launch)
        if a.command=="apps": result=service.applications()
        elif a.command=="check":
            for app in service.applications(): service.load(app["id"])
            result={"status":"valid","version":__version__,"applications":len(service.applications())}
        elif a.command=="profiles":
            if a.profiles_command=="list": result=service.profile_list(a.workflow)
            else:
                if not a.developer: raise Fault("developer_disabled")
                result=_import_legacy_profile(service,a.workflow,a.file)
        elif a.command=="providers": result=service.verify_provider(a.profile,a.workflow)
        elif a.command=="run":
            data=_json_file(a.input) if a.input else {}
            mode="demo" if a.example else a.input_mode
            overrides={k:v for k,v in {"output_repair_retries":a.output_repair_retries,"semantic_revision_retries":a.semantic_revision_retries}.items() if v is not None}; rid=service.start(a.workflow,a.profile,mode,data,guideline_selection={"set_id":a.guideline_set,"selector":a.guideline_version},example=a.example,developer=a.developer,retry_overrides=overrides); result=service.runner.advance(rid)
        elif a.command=="status": result=service.inspect(a.run_id,developer=a.developer)
        elif a.command=="resume": result=service.resume(a.run_id)
        elif a.command=="cancel": service.runner.cancel(a.run_id); result=service.store.run(a.run_id)
        elif a.command=="delete": service.delete(a.run_id); result={"deleted":True}
        elif a.command=="review":
            payload={"review_request_id":a.review_request_id or str(uuid.uuid4()),"revision":a.revision,"actor":a.actor,"decision":a.decision,"comments":a.comments}
            if a.decision=="approve": payload.update(acknowledged_omission_ids=a.ack_omission,acknowledged_conflict_ids=a.ack_conflict)
            if a.target: payload["target"]=a.target
            if a.decision=="revise":
                state=service.store.run(a.run_id)
                if state.get("status")=="blocked": payload["expected_block"]={k:state.get("block",{}).get(k) for k in ["code","node_id","attempt"]}
            result=service.review(a.run_id,payload)
        elif a.command=="guidelines":
            if a.guidelines_command=="list": result=service.guidelines.list(developer=a.developer)
            elif not a.developer: raise Fault("developer_disabled")
            elif a.guidelines_command=="import":
                ingestion=yaml.safe_load(Path(a.ingestion_profile).read_text(encoding="utf-8")) if a.ingestion_profile else None
                result=service.import_guideline_devel(a.set_id,_json_file(a.sources),ingestion,a.profile,developer=True)
            else: result=service.guidelines.promote(a.set_id,a.expected_snapshot,a.note,a.actor)
        elif a.command=="steps":
            if not a.developer: raise Fault("developer_disabled")
            if a.steps_command=="list": result=service.steps(a.workflow)
            else:
                fixture=service.fixtures.load(a.fixture); tape=service.fixtures.load_tape(a.tape,fixture) if a.tape else None
                overrides={k:v for k,v in {"output_repair_retries":a.output_repair_retries,"semantic_revision_retries":a.semantic_revision_retries}.items() if v is not None}; rid=service.run_step(a.workflow,a.node,fixture,a.profile,tape=tape,developer=True,retry_overrides=overrides); result=service.runner.advance(rid)
        elif a.command=="fixtures":
            if not a.developer: raise Fault("developer_disabled")
            if a.fixtures_command=="capture":
                doc=service.capture_fixture(a.run_id,a.node,a.attempt); Path(a.output).write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8"); result={"output":a.output}
            else:
                doc=service.fixtures.load(a.file); service._validate_fixture_for_step(doc)
                path=service.fixtures.promote(doc,a.id,a.version,a.suitability,a.acknowledge_reviewed,a.actor,doc["workflow_id"],doc["node_id"]); result={"path":str(path)}
        elif a.command=="self":
            if not a.developer: raise Fault("developer_disabled")
            if a.self_command=="export":
                doc=service.self_handoff(a.run_id); Path(a.output).write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8"); result={"output":a.output}
            else: result=service.self_submit(a.run_id,_json_file(a.response))
        else:
            from .web import create_app
            create_app(service).run(host="127.0.0.1",port=a.port,debug=False,threaded=True); return 0
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 1 if isinstance(result,dict) and result.get("status") in {"failed","blocked"} else 0
    except (Fault,OSError,ValueError,KeyError) as exc:
        print(json.dumps({"error":present_error(exc)},ensure_ascii=False),file=sys.stderr); return 1
    finally:
        if service is not None and a.command!="serve": service.close()
if __name__=="__main__": raise SystemExit(main())
