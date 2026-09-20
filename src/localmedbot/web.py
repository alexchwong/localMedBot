"""Local-only browser surface; same service and gates as the CLI."""
from concurrent.futures import ThreadPoolExecutor
import secrets
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, render_template
from werkzeug.exceptions import HTTPException
from .contracts import Fault
from .knowledge import Knowledge


def create_app(service):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    token = secrets.token_urlsafe(32)
    pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix="workflow")
    app.extensions["localmedbot_pool"] = pool
    app.extensions["localmedbot_token"] = token
    pending = {}

    @app.before_request
    def local_boundary():
        hostname = request.host.split(":")[0]
        if hostname not in ["localhost","127.0.0.1"]:
            raise Fault("host_denied")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                raise Fault("origin_denied")
            if not secrets.compare_digest(request.headers.get("X-LocalMedBot-Token",""),token):
                raise Fault("request_token")
            if not request.is_json:
                raise Fault("json_required")

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Fault)
    def failure(error):
        return jsonify(error={"code":error.code,"detail":error.detail,"findings":error.findings}),409 if error.code == "stale_review" else 400

    @app.errorhandler(HTTPException)
    def http_failure(error):
        return jsonify(error={"code":"http_error","status":error.code}),error.code

    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    @app.errorhandler(ValueError)
    def invalid_payload(error):
        return jsonify(error={"code":"invalid_input"}),400

    @app.get("/")
    def home():
        return render_template("index.html",token=token,provider=service.model["provider"])

    @app.get("/api/applications")
    def applications():
        return jsonify(service.applications())

    @app.get("/api/examples/<app_id>/<example>")
    def example(app_id,example):
        return jsonify(service.example(app_id,example)["input"])

    @app.get("/api/runs")
    def runs():
        return jsonify([{"id":r["id"],"status":r["status"],"application":r["snapshot"]["manifest"]["name"],"created":r["created"]} for r in service.store.runs()])

    def launch(rid):
        if rid not in pending or pending[rid].done():
            pending[rid] = pool.submit(service.runner.advance,rid)

    @app.post("/api/runs")
    def start():
        data=request.get_json()
        rid=service.start(data["application"],data.get("input"),data.get("example","standard"))
        launch(rid)
        return jsonify(id=rid),202

    @app.get("/api/runs/<rid>")
    def inspect(rid):
        future = pending.get(rid)
        if future and future.done() and future.exception():
            return jsonify(error={"code":"execution_interrupted","detail":"Inspect audit events and resume the run."}),500
        return jsonify(service.inspect(rid))

    @app.post("/api/runs/<rid>/resume")
    def resume(rid):
        service.store.run(rid)
        launch(rid)
        return jsonify(id=rid),202

    @app.post("/api/runs/<rid>/review")
    def review(rid):
        d=request.get_json()
        run=service.runner.review(rid,d["revision"],d["actor"],d["decision"],d.get("comments",""))
        return jsonify(status=run["status"])

    @app.post("/api/ingest/<app_id>")
    def ingest(app_id):
        data=request.get_json()
        return jsonify(corpus_id=service.ingest(app_id,data.get("sources")))

    @app.get("/api/runs/<rid>/source/<cid>/<eid>")
    def source(rid,cid,eid):
        r=service.store.run(rid)
        return jsonify(Knowledge(service.store,r["corpora"],r["snapshot"]["policy"].get("overlay")).read(cid,eid))

    return app
