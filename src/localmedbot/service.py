"""Shared application service used by CLI and local browser."""
from copy import deepcopy
from pathlib import Path
import json
import yaml
from .compiler import asset, load_application
from .contracts import Fault
from .storage import Store
from .runtime import Runner
from .modules import registry
from .knowledge import prepare_sources


class Service:
    def __init__(self, apps="applications", data=".localmedbot", model=None):
        self.apps = Path(apps).resolve()
        self.store = Store(data)
        self.registry = registry()
        self.runner = Runner(self.store,self.registry)
        self.model = model or {"provider":"recorded","max_tokens":2048}

    def applications(self):
        return [yaml.safe_load(p.read_text()) for p in sorted(self.apps.glob("*/application.yaml"))]

    def root(self, app):
        matches = [p.parent for p in self.apps.glob("*/application.yaml") if yaml.safe_load(p.read_text())["id"] == app]
        if len(matches) != 1:
            raise Fault("application_not_found")
        return matches[0]

    def load(self, app):
        root = self.root(app)
        snap = load_application(root,self.registry)
        snap["input_schema"] = json.loads(asset(root,snap["manifest"]["input_schema"]).read_text())
        return snap

    def example(self, app, name):
        root = self.root(app)
        if not name or "/" in name or "\\" in name or ".." in name:
            raise Fault("example_name")
        return json.loads(asset(root,f"examples/{name}.json").read_text())

    def ingest(self, app, sources=None):
        with self.runner.lock:
            root = self.root(app)
            snap = self.load(app)
            profile = yaml.safe_load(asset(root,snap["manifest"]["ingestion"]).read_text())
            sources = sources if sources is not None else json.loads(asset(root,snap["manifest"]["corpus"]).read_text())
            if profile.get("mode") == "model":
                workflow = {"nodes":[{"id":"import","module":"ingest","inputs":{"data":"run.input"},"config":{"profile":profile,"scope":"application","name":"knowledge"},"repairs":1}],"output":"import"}
                import_snap = {"manifest":snap["manifest"],"workflow":workflow,"policy":{**snap["policy"],"required_checks":[],"human_approval":False}}
                rid = self.runner.create(import_snap,{"sources":sources},self.model)
                run = self.runner.advance(rid)
                if run["status"] != "completed":
                    raise Fault("ingestion_failed",run.get("error",{}).get("code",""))
                return self.store.artifact(rid,"import")["payload"]["corpus_id"]
            items = prepare_sources(sources,profile)
            return self.store.publish(app,"knowledge",profile,sources,items)

    def start(self, app, input_value=None, example="standard"):
        with self.runner.lock:
            snap = self.load(app)
            root = self.root(app)
            if input_value is None or self.model["provider"] == "recorded":
                fixture = self.example(app,example)
                if input_value is not None and input_value != fixture["input"] and self.model["provider"] == "recorded":
                    raise Fault("recorded_input_mismatch", "Choose HTTP mode for custom inputs")
                input_value = fixture["input"] if input_value is None else input_value
                snap["recording"] = fixture["responses"]
            corpora = []
            if snap["manifest"].get("corpus"):
                cid = self.store.active_corpus(app,"knowledge") or self.ingest(app)
                corpora.append(cid)
            # Embed the input ingestion profile; these assets are also frozen.
            for node in snap["workflow"]["nodes"]:
                if node["module"] == "ingest":
                    node["config"]["profile"] = yaml.safe_load(asset(root,node["config"]["profile"]).read_text())
            rid = self.runner.create(snap,input_value,self.model,corpora)
            return rid

    def inspect(self, rid):
        run = self.store.run(rid)
        artifacts = {node:self.store.artifact(rid,node,rev) for node,rev in run["active"].items()}
        return {"run":run,"artifacts":artifacts,"events":self.store.events(rid)}
