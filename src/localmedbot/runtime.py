"""Application-neutral sequential scheduler with durable attempts and gates."""
from copy import deepcopy
import json
import time
import threading
from .contracts import Fault, validate, parse
from .compiler import compile_workflow, artifact_ref
from .knowledge import Knowledge
from .storage import uid
from .providers import make_provider
from . import __version__


def dig(value, path):
    for part in path.split('.') if path else []:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class Context:
    def __init__(self, runner, run, node, attempt):
        self.runner, self.store, self.run, self.node = runner, runner.store, run, node
        self.attempt, self.call_index = attempt, 0
        self.started = time.monotonic()
        self.base_seconds = run["usage"]["seconds"]
        self.base_node_seconds = run["node_usage"][node["id"]]["seconds"]
        self.knowledge = Knowledge(self.store, run["corpora"], run["snapshot"]["policy"].get("overlay"))

    def save(self):
        elapsed = time.monotonic() - self.started
        self.run["usage"]["seconds"] = self.base_seconds + elapsed
        self.run["node_usage"][self.node["id"]]["seconds"] = self.base_node_seconds + elapsed
        self.store.put_run(self.run)

    def event(self, kind, data):
        self.store.event(self.run["id"], kind, {"node": self.node["id"], "attempt": self.attempt, **data})

    def reserve(self, **amounts):
        self.save()
        limits = self.run["snapshot"]["policy"]["limits"]
        for field in ["seconds", *amounts]:
            amount = amounts.get(field, 0)
            if self.run["usage"][field] + amount > limits[field] or self.run["node_usage"][self.node["id"]][field] + amount > limits["node_"+field]:
                raise Fault("budget_exhausted", field)
        for field, value in amounts.items():
            self.run["usage"][field] += value
            self.run["node_usage"][self.node["id"]][field] += value
        self.save()

    def call(self, prompt, payload, schema, role="reasoning"):
        self.call_index += 1
        key = f"{self.node['id']}:{self.attempt}:{self.call_index}"
        if key in self.run["responses"]:
            self.event("response_replayed", {"key": key})
            return self.run["responses"][key]
        cfg = self.run["model"]
        roles = cfg.get("roles", {})
        settings = {**cfg, **roles.get(role, {})}
        messages = [{"role": "system", "content": prompt + "\nReturn JSON matching this schema:\n" + json.dumps(schema)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        maximum = int(settings.get("max_tokens", 2048))
        # Byte count is intentionally conservative and not advertised as exact token accounting.
        reservation = len(json.dumps(messages).encode()) + maximum
        provider = make_provider(settings, self.run["snapshot"].get("recording"))
        for retry in range(self.run["snapshot"]["policy"].get("transport_retries", 1) + 1):
            self.reserve(turns=1, tokens=reservation)
            limits = self.run["snapshot"]["policy"]["limits"]
            timeout = max(.01, min(settings.get("timeout", 45), limits["seconds"] - self.run["usage"]["seconds"], limits["node_seconds"] - self.run["node_usage"][self.node["id"]]["seconds"]))
            request = {"messages": messages, "max_tokens": maximum, "timeout": timeout}
            index = self.run["provider_offsets"].get(self.node["id"], 0)
            self.run["provider_offsets"][self.node["id"]] = index + 1
            self.save()
            self.event("model_request", {"request": request, "role": role, "model": settings.get("model"), "provider": settings["provider"], "reservation": reservation})
            try:
                response = provider.complete(request, self.node["id"], index)
                self.reserve()
                self.event("model_response", response)
                if len(response["text"].encode()) > 100_000:
                    raise Fault("response_too_large")
                self.run["responses"][key] = response["text"]
                self.save()
                return response["text"]
            except Fault as exc:
                self.event("model_error", {"code": exc.code})
                if exc.code != "transport_retryable" or retry >= self.run["snapshot"]["policy"].get("transport_retries", 1):
                    raise

    def tool(self, name, args):
        permitted = set(self.run["snapshot"]["policy"].get("tools", [])) & set(self.node.get("config", {}).get("tools", []))
        if name not in permitted:
            raise Fault("tool_denied", name)
        if not isinstance(args, dict):
            raise Fault("tool_arguments")
        self.reserve(tool_calls=1)
        self.event("tool_request", {"tool": name, "arguments": args})
        if name == "evidence.search":
            if set(args) - {"query", "filters", "limit", "mode"}:
                raise Fault("tool_arguments")
            result = self.knowledge.search(**args)
        elif name == "evidence.read":
            if set(args) != {"corpus_id", "evidence_id"}:
                raise Fault("tool_arguments")
            result = self.knowledge.read(args["corpus_id"], args["evidence_id"])
        else:
            raise Fault("tool_denied", name)
        self.event("tool_result", {"tool": name, "result": result})
        return result


class Runner:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.lock = threading.RLock()

    def create(self, snapshot, input_value, model, corpora=None, rid=None):
        snapshot = compile_workflow(deepcopy(snapshot), self.registry)
        validate(input_value, snapshot.get("input_schema", {}))
        # Whitelist configuration; credentials are environment-only.
        allowed = {"provider","base_url","model","api_key_env","temperature","max_tokens","timeout","json_mode","roles"}
        if set(model) - allowed:
            raise Fault("model_config_field")
        for rolecfg in model.get("roles", {}).values():
            if set(rolecfg) - (allowed - {"roles"}):
                raise Fault("model_config_field")
        make_provider(model, snapshot.get("recording"))
        run = {"id": rid or uid(), "status": "pending", "input": input_value, "snapshot": snapshot, "model": deepcopy(model),
               "corpora": list(corpora or []), "nodes": {}, "active": {}, "attempts": {}, "feedback": {}, "cycles": {},
               "responses": {}, "provider_offsets": {}, "usage": {"turns":0,"tokens":0,"tool_calls":0,"seconds":0},
               "node_usage": {}, "approval": None, "created": time.time(), "runtime_version": __version__}
        for n in snapshot["workflow"]["nodes"]:
            run["nodes"][n["id"]] = "pending"
            run["node_usage"][n["id"]] = {"turns":0,"tokens":0,"tool_calls":0,"seconds":0}
        self.store.put_run(run)
        self.store.event(run["id"], "created", {"provider": model["provider"], "corpora": run["corpora"]})
        return run["id"]

    def resolve(self, run, ref):
        if ref == "run.input":
            return run["input"]
        if ref.startswith("feedback."):
            return run["feedback"].get(ref.split(".",1)[1])
        if ref.startswith("artifacts."):
            parent, path = artifact_ref(ref, run["nodes"])
            if parent in run["active"]:
                value = self.store.artifact(run["id"],parent,run["active"][parent])["payload"]
                return dig(value, path)
        return None

    def invalidate(self, run, target):
        invalid = {target}
        for n in run["snapshot"]["workflow"]["nodes"]:
            if n["id"] == target or invalid & set(n.get("needs", [])):
                invalid.add(n["id"])
                run["active"].pop(n["id"], None)
                run["nodes"][n["id"]] = "pending"
        run["approval"] = None
        self.store.event(run["id"], "invalidated", {"nodes": sorted(invalid)})

    def finish_node(self, run, node, payload, outcome):
        """Resume-safe post-commit control transitions, never model execution."""
        nid = node["id"]
        review = node.get("review")
        if review and payload.get("status") != "pass":
            count = run["cycles"].get(nid, 0)
            if count >= review["max_revisions"]:
                raise Fault("review_exhausted", nid, payload.get("findings"))
            run["cycles"][nid] = count + 1
            run["feedback"][review["target"]] = payload
            self.invalidate(run, review["target"])
        if outcome == "waiting_review":
            self.ensure_checks(run)
            run["status"] = "waiting_review"
        run.pop("postprocess", None)

    def advance(self, rid):
        with self.lock:
            run = self.store.run(rid)
            if run.get("runtime_version") != __version__:
                raise Fault("runtime_version_mismatch")
            if run["status"] in ["completed", "waiting_review", "cancelled", "blocked"]:
                return run
            run["status"] = "running"
            self.store.put_run(run)
            nodes = run["snapshot"]["workflow"]["nodes"]
            while True:
                postprocess = run.get("postprocess")
                if postprocess:
                    node = next(n for n in nodes if n["id"] == postprocess["node"])
                    payload = self.store.artifact(rid, node["id"], run["active"][node["id"]])["payload"]
                    try:
                        self.finish_node(run, node, payload, postprocess["outcome"])
                    except Fault as exc:
                        run["status"] = "blocked"
                        run["error"] = {"code": exc.code, "detail": exc.detail}
                    self.store.put_run(run)
                    if run["status"] in ["waiting_review", "blocked"]:
                        return run
                pending = [n for n in nodes if run["nodes"][n["id"]] not in ["complete", "skipped"]]
                if not pending:
                    if run["snapshot"]["policy"].get("human_approval") and not run["approval"]:
                        self.ensure_checks(run)
                        run["status"] = "waiting_review"
                    else:
                        run["status"] = "completed"
                    self.store.put_run(run); return run
                node = pending[0]
                nid = node["id"]
                cond = node.get("when")
                if cond:
                    v = self.resolve(run, cond["from"])
                    applies = v is not None if cond["op"] == "exists" else v == cond.get("value") if cond["op"] == "equals" else v in cond.get("value", [])
                    if not applies:
                        run["nodes"][nid] = "skipped"
                        self.store.put_run(run); continue
                resuming = run["nodes"][nid] == "running"
                if not resuming:
                    run["attempts"][nid] = run["attempts"].get(nid, 0) + 1
                else:
                    self.store.event(rid, "interrupted_attempt_resumed", {"node":nid, "external_call_may_repeat":True})
                run["nodes"][nid] = "running"
                self.store.put_run(run)
                ctx = Context(self, run, node, run["attempts"][nid])
                try:
                    inputs = {}
                    refs = {}
                    for name, b in node.get("inputs", {}).items():
                        spec = b if isinstance(b, dict) else {"from":b}
                        value = self.resolve(run, spec["from"])
                        if value is None and not spec.get("optional"):
                            raise Fault("required_input_missing", name)
                        inputs[name] = value
                        if spec["from"].startswith("artifacts."):
                            parent = artifact_ref(spec["from"], run["nodes"])[0]
                            refs[parent] = run["active"].get(parent)
                    if nid in run["feedback"]:
                        inputs["revision_feedback"] = run["feedback"][nid]
                    ctx.reserve()
                    result = self.registry.get(node["module"]).execute(ctx, inputs, node.get("config", {}))
                    validate(result.payload, node.get("schema", {}))
                    ctx.reserve()
                    run["postprocess"] = {"node": nid, "outcome": result.outcome}
                    self.store.commit(run, nid, result.payload, {"attempt":ctx.attempt,"inputs":refs,"schema":node.get("schema",{}),"corpora":list(run["corpora"])})
                    ctx.event("node_complete", {"revision":run["active"][nid]})
                    self.finish_node(run, node, result.payload, result.outcome)
                    if run["status"] == "waiting_review":
                        ctx.save(); return run
                    ctx.save()
                except Fault as exc:
                    ctx.event("node_error", {"code":exc.code,"detail":exc.detail,"findings":exc.findings})
                    repairs = run.setdefault("repair_counts", {}).get(nid, 0)
                    if exc.code in ["syntax_invalid", "schema_invalid", "reference_invalid"] and repairs < node.get("repairs", 0):
                        run["repair_counts"][nid] = repairs + 1
                        run["feedback"][nid] = {"code":exc.code,"findings":exc.findings}
                        run["nodes"][nid] = "pending"
                        ctx.save(); continue
                    run["status"] = "failed" if exc.code.startswith("transport") else "blocked"
                    run["error"] = {"code":exc.code,"detail":exc.detail}
                    ctx.save(); return run

    def ensure_checks(self, run):
        for nid in run["snapshot"]["policy"].get("required_checks", []):
            if nid not in run["active"] or self.store.artifact(run["id"],nid,run["active"][nid])["payload"].get("status") != "pass":
                raise Fault("checks_not_passed", nid)

    def review(self, rid, revision, actor, decision, comments=""):
        with self.lock:
            run = self.store.run(rid)
            output = run["snapshot"]["workflow"]["output"]
            if run["active"].get(output) != revision:
                raise Fault("stale_review")
            if not isinstance(actor,str) or not actor.strip():
                raise Fault("actor_required")
            if decision not in ["approve", "reject", "revise"]:
                raise Fault("review_action")
            if run["status"] not in ["waiting_review", "completed"]:
                raise Fault("not_reviewable")
            self.ensure_checks(run)
            event = {"revision":revision,"actor":actor,"decision":decision,"comments":comments,"time":time.time()}
            self.store.event(rid,"human_review",event)
            if decision == "approve":
                run["approval"] = event
                run["status"] = "completed"
            elif decision == "reject":
                run["approval"] = None; run["status"] = "blocked"
                run["error"] = {"code":"human_rejected"}
            else:
                target = run["snapshot"]["workflow"]["revision_target"]
                self.invalidate(run,target)
                run["feedback"][target] = {"comments":comments}
                run["status"] = "pending"
            self.store.put_run(run)
            return run

    def cancel(self, rid):
        with self.lock:
            run = self.store.run(rid); run["status"] = "cancelled"; run["approval"] = None
            self.store.put_run(run); self.store.event(rid,"cancelled",{})
