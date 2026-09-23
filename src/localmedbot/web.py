"""Local browser surface over the shared Service contract."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import secrets,uuid,logging,threading
from flask import Flask,request,jsonify,render_template,g,send_file
from werkzeug.exceptions import HTTPException
from .contracts import Fault
from .knowledge import Knowledge
from .errors import present_error
from .runtime import recovery_action
from . import __version__

_MUTATING={"POST","PUT","PATCH","DELETE"}

def create_app(service):
    app=Flask(__name__); app.config["MAX_CONTENT_LENGTH"]=2*1024*1024
    sessions={}; pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="workflow"); pending={}; pending_lock=threading.Lock()
    app.extensions.update(localmedbot_pool=pool,localmedbot_sessions=sessions)

    def session(): return sessions[g.session_id]
    @app.before_request
    def boundary():
        host=request.host.split(":")[0]
        if host not in {"localhost","127.0.0.1"}: raise Fault("host_denied")
        sid=request.cookies.get("localmedbot_session")
        if not sid or sid not in sessions:
            sid=secrets.token_hex(32); sessions[sid]={"csrf":secrets.token_urlsafe(32),"developer_enabled":False,"copies":{}}; g.new_session=True
        else:g.new_session=False
        g.session_id=sid
        if request.method in _MUTATING:
            origin=request.headers.get("Origin")
            if origin and origin.rstrip("/")!=request.host_url.rstrip("/"): raise Fault("origin_denied")
            if not secrets.compare_digest(request.headers.get("X-LocalMedBot-Token",""),session()["csrf"]): raise Fault("request_token")
            if request.method!="DELETE" and not request.is_json: raise Fault("json_required")
    @app.after_request
    def response_headers(resp):
        resp.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        resp.headers["X-Content-Type-Options"]="nosniff"; resp.headers["Cache-Control"]="no-store"
        if getattr(g,"new_session",False): resp.set_cookie("localmedbot_session",g.session_id,httponly=True,samesite="Strict",secure=request.is_secure,path="/")
        return resp
    def dev_required():
        if not session()["developer_enabled"]: raise Fault("developer_disabled")
    def body(allowed,required=()):
        d=request.get_json()
        if not isinstance(d,dict) or set(d)-set(allowed) or any(k not in d for k in required): raise Fault("invalid_input")
        return d
    def launch(rid,operation=None):
        with pending_lock:
            if rid in pending and not pending[rid].done(): raise Fault("run_active")
            try:
                future=pool.submit(operation or service.runner.advance,rid)
            except Exception:
                app.logger.error("Execution submission failed for %s",rid)
                service.store.stop_execution(rid,"execution_dispatch_failed",uuid.uuid4().hex[:12])
                raise Fault("execution_dispatch_failed") from None
            pending[rid]=future
        def completed(done):
            with pending_lock:
                if pending.get(rid) is not done: return
                try:
                    done.result()
                    run=service.store.run(rid)
                    if run["status"] in {"pending","running"}:
                        service.store.stop_execution(rid,"execution_stopped",uuid.uuid4().hex[:12],run["inspection_revision"])
                except Exception:
                    reference=uuid.uuid4().hex[:12]
                    # Never log exception payloads: provider messages can contain secrets or patient text.
                    app.logger.error("Execution worker failed for %s (diagnostic %s)",rid,reference)
                    try: service.store.stop_execution(rid,"execution_stopped",reference)
                    except Exception: app.logger.error("Execution reconciliation unavailable (diagnostic %s)",reference)
        future.add_done_callback(completed)

    @app.errorhandler(Fault)
    def fault(exc):
        code=exc.code; status=403 if code in {"developer_disabled","scope_denied"} else 404 if code.endswith("_not_found") else 413 if code=="input_too_large" else 409 if code.startswith("stale_") or code in {"review_request_conflict","response_already_submitted","run_active","run_not_active","review_revision_required","run_not_resumable"} else 400
        return jsonify(error=present_error(exc)),status
    @app.errorhandler(HTTPException)
    def http_error(exc): return jsonify(error={**present_error({"code":"http_error","detail":str(exc)}),"status":exc.code}),exc.code
    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    @app.errorhandler(ValueError)
    def invalid(exc): return jsonify(error=present_error({"code":"invalid_input","detail":str(exc)})),400

    @app.errorhandler(Exception)
    def unexpected(exc):
        # Do not expose tracebacks or guess at a cause. The shared formatter
        # supplies a stable diagnostic reference for logs/support.
        error=present_error(exc)
        logging.getLogger('localmedbot.web').error('Unhandled HTTP application error (%s; %s)',type(exc).__name__,error.get('diagnostic_reference','unavailable'))
        return jsonify(error=error),500

    @app.get("/")
    def home(): return render_template("index.html",token=session()["csrf"],version=__version__)
    @app.get("/api/session")
    def session_state(): return jsonify(developer_enabled=session()["developer_enabled"],csrf=session()["csrf"])
    @app.post("/api/developer-mode")
    def developer_mode():
        d=body({"enabled"},{"enabled"})
        if not isinstance(d["enabled"],bool): raise Fault("invalid_input")
        session()["developer_enabled"]=d["enabled"]; return jsonify(developer_enabled=session()["developer_enabled"])
    @app.get("/api/runtime")
    def runtime_info(): return jsonify(version=__version__,state_root=str(service.paths.state_root),runs_root=str(service.paths.runs_root),config_root=str(service.paths.config_root),execution_defaults=service.execution_defaults)
    @app.get("/api/applications")
    def applications(): return jsonify(service.applications())
    @app.get("/api/examples/<app_id>/<example>")
    def example(app_id,example): return jsonify(service.example(app_id,example)["input"])
    @app.get("/api/model-profiles")
    def profiles(): return jsonify(service.profile_list(request.args.get("workflow_id")))
    @app.post("/api/model-profiles/configure")
    def configure_profile():
        d=body({"profile_id","overlay","credential","clear_credential"},{"profile_id"}); effective=service.configure_profile(d["profile_id"],d.get("overlay",{}),d.get("credential"),bool(d.get("clear_credential",False)))
        return jsonify(profile_id=effective["id"],executor=effective["executor"],base_url=effective.get("base_url"),model=effective.get("model"),destination=effective["destination"],credential_present=bool(service.profiles.credential(effective)))
    @app.post("/api/providers/verify")
    def verify():
        d=body({"profile_id","workflow_id","profile_overrides"},{"profile_id","workflow_id"}); return jsonify(service.verify_provider(d["profile_id"],d["workflow_id"],d.get("profile_overrides")))
    @app.get("/api/guideline-sets")
    def guideline_sets(): return jsonify(service.guidelines.list(developer=session()["developer_enabled"]))
    @app.get("/api/runs")
    def runs():
        rows=[]
        for r in service.store.runs():
            workflow=r.get("snapshot",{}).get("manifest",{})
            if request.args.get("workflow_id") and workflow.get("id")!=request.args["workflow_id"]: continue
            rows.append({"id":r["id"],"status":r.get("status","legacy"),"application":workflow.get("name","Legacy run"),"workflow_id":workflow.get("id"),"title":r.get("title"),"created":r.get("created"),"inspection_revision":r.get("inspection_revision"),"legacy":"run_contract_version" not in r})
        rows.sort(key=lambda r:(r["created"] or 0,r["id"]),reverse=True)
        return jsonify(rows)
    @app.post("/api/runs")
    def start():
        d=body({"workflow_id","profile_id","input_mode","input","profile_overrides","guideline_selection","example","copy_input_id","retry_overrides","title"},{"workflow_id","profile_id"}); mode=d.get("input_mode","free_text"); input_data=d.get("input",{}); derived=None; origin=None
        if mode=="advanced": dev_required()
        if mode=="copied":
            token=d.get("copy_input_id"); copied=session()["copies"].get(token)
            if not copied: raise Fault("copy_input_invalid")
            input_data=copied["input"]; d["workflow_id"]=copied["workflow_id"]; derived=copied["derived_from_run_id"]; origin=copied["origins"]
            if d["workflow_id"]=="guideline_qa":
                sel=d.get("guideline_selection") or {}
                if not isinstance(sel.get("set_id"),str) or not sel.get("set_id") or not isinstance(sel.get("selector"),str) or not sel.get("selector"): raise Fault("guideline_selection_required")
        rid=service.start(d["workflow_id"],d["profile_id"],mode,input_data,d.get("profile_overrides"),d.get("guideline_selection"),d.get("example"),developer=session()["developer_enabled"],derived_from_run_id=derived,origin_override=origin,retry_overrides=d.get("retry_overrides"),title=d.get("title"))
        if mode=="copied": session()["copies"].pop(d["copy_input_id"],None)
        launch(rid); return jsonify(id=rid),202
    @app.get("/api/runs/<rid>")
    def inspect(rid): return jsonify(service.inspect(rid,developer=session()["developer_enabled"]))
    @app.post("/api/runs/<rid>/resume")
    def resume(rid):
        d=body({"acknowledge_external_retry"})
        action=recovery_action(service.store.run(rid))
        if action is None: raise Fault("run_not_resumable")
        if action=="explicit_external_retry" and d.get("acknowledge_external_retry") is not True: raise Fault("external_retry_acknowledgement_required")
        launch(rid,lambda run_id: service.resume(run_id,acknowledge_external_retry=d.get("acknowledge_external_retry") is True))
        return jsonify(id=rid),202
    @app.post("/api/runs/<rid>/cancel")
    def cancel(rid): body(set()); service.runner.cancel(rid); return jsonify(status=service.store.run(rid)["status"])
    @app.post("/api/runs/<rid>/review")
    def review(rid):
        payload=body({"review_request_id","revision","actor","decision","comments","target","omissions","acknowledged_omission_ids","acknowledged_conflict_ids","expected_block"},{"review_request_id","actor","decision"})
        prior=service.store.review_event(rid,payload["review_request_id"])
        run=service.review(rid,payload)
        if run["status"]=="pending" and not prior: launch(rid)
        return jsonify(status=run["status"])
    @app.delete("/api/runs/<rid>")
    def delete(rid):
        with pending_lock:
            if rid in pending and not pending[rid].done(): raise Fault("run_active")
            service.delete(rid)
        return jsonify(deleted=True)
    @app.get("/api/runs/<rid>/download/<path:relpath>")
    def download(rid,relpath):
        path=service.store.download_path(rid,relpath); return send_file(path,as_attachment=True,download_name=path.name)
    @app.post("/api/runs/<rid>/copy-input")
    def copy_input(rid):
        body(set()); copied=service.legacy_copy_payload(rid); token=uuid.uuid4().hex
        session()["copies"][token]={k:copied[k] for k in ["workflow_id","input","derived_from_run_id","origins"]}
        return jsonify(copy_input_id=token,workflow_id=copied["workflow_id"],input=copied["input"],old_metadata=copied["old_metadata"])
    @app.get("/api/runs/<rid>/source/<cid>/<eid>")
    def source(rid,cid,eid):
        r=service.store.run(rid); return jsonify(Knowledge(service.store,r.get("corpora",[]),r.get("snapshot",{}).get("policy",{}).get("overlay")).read(cid,eid))

    @app.get("/api/dev/workflows/<workflow>/steps")
    def steps(workflow): dev_required(); return jsonify(service.steps(workflow))
    @app.post("/api/dev/step-runs")
    def step_run():
        dev_required(); d=body({"workflow_id","node_id","profile_id","fixture","profile_overrides","tape","configuration_source","retry_overrides"},{"workflow_id","node_id","profile_id","fixture"}); fixture=d["fixture"]; tape=d.get("tape")
        rid=service.run_step(d["workflow_id"],d["node_id"],fixture,d["profile_id"],d.get("profile_overrides"),tape=tape,configuration_source=d.get("configuration_source","current"),developer=True,retry_overrides=d.get("retry_overrides")); launch(rid); return jsonify(id=rid),202
    @app.get("/api/dev/fixtures")
    def list_fixtures(): dev_required(); return jsonify(service.list_fixtures())
    @app.post("/api/dev/fixtures")
    def save_fixture():
        dev_required(); d=body({"fixture"},{"fixture"}); return jsonify(path=service.save_scratch_fixture(d["fixture"]))
    @app.post("/api/dev/fixtures/capture")
    def capture_fixture(): dev_required(); d=body({"run_id","node_id","attempt"},{"run_id","node_id","attempt"}); return jsonify(service.capture_fixture(d["run_id"],d["node_id"],int(d["attempt"])))
    @app.post("/api/dev/fixtures/<fixture_id>/promote")
    def promote_fixture(fixture_id):
        dev_required(); d=body({"version","new_id","new_version","suitability","acknowledge_reviewed","actor"},{"version","new_id","new_version","suitability","actor"}); path=service.promote_fixture(fixture_id,int(d["version"]),d["new_id"],int(d["new_version"]),d["suitability"],bool(d.get("acknowledge_reviewed")),d["actor"]); return jsonify(path=path)
    @app.delete("/api/dev/fixtures/<fixture_id>")
    def delete_fixture(fixture_id):
        dev_required(); version=request.args.get("version",type=int)
        if version is None: raise Fault("fixture_version_invalid")
        service.delete_scratch_fixture(fixture_id,version); return jsonify(deleted=True)
    @app.get("/api/dev/runs/<rid>/handoff")
    def handoff(rid): dev_required(); return jsonify(service.self_handoff(rid))
    @app.post("/api/dev/runs/<rid>/handoff")
    def submit_handoff(rid): dev_required(); d=body({"contract_version","request_id","content"},{"contract_version","request_id","content"}); return jsonify(status=service.self_submit(rid,d)["status"])
    @app.post("/api/dev/guideline-sets/<set_id>/import")
    def dev_import(set_id):
        dev_required(); d=body({"sources","ingestion_profile","profile_id"},{"sources"}); return jsonify(service.import_guideline_devel(set_id,d["sources"],d.get("ingestion_profile"),d.get("profile_id"),developer=True))
    return app
