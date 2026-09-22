"""Versioned portable step fixtures and recorded tape pairing."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json,re,shutil
from .contracts import Fault,validate_actor
from .storage import encode

_FIX_RE=re.compile(r"^[a-z0-9](?:[a-z0-9_-]*)(?:\.[a-z0-9][a-z0-9_-]*)*$")

def validate_fixture_id(value):
    if not isinstance(value,str) or not 1<=len(value)<=128 or not _FIX_RE.fullmatch(value): raise Fault("fixture_id_invalid")
    return value

def validate_version(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=2147483647: raise Fault("fixture_version_invalid")
    return value

class FixtureManager:
    def __init__(self,store,repo_root,scratch_root):
        self.store=store; self.repo_root=Path(repo_root).resolve(); self.scratch=Path(scratch_root).resolve(); self.scratch.mkdir(parents=True,exist_ok=True)
    def filename(self,fixture_id,version): return f"fixture-{validate_fixture_id(fixture_id)}.v{validate_version(version)}.json"
    def validate_envelope(self,doc):
        allowed={"fixture_schema_version","id","version","workflow_id","node_id","step_contract_version","resolved_inputs","evidence_snapshots","context","provenance","data_suitability","origins","captured_configuration"}
        required=allowed-{"captured_configuration"}
        if not isinstance(doc,dict) or set(doc)-allowed or not required<=set(doc) or doc.get("fixture_schema_version")!=1 or doc.get("step_contract_version")!=1:
            raise Fault("fixture_contract_invalid")
        validate_fixture_id(doc.get("id")); validate_version(doc.get("version"))
        if not isinstance(doc.get("workflow_id"),str) or not isinstance(doc.get("node_id"),str) or not isinstance(doc.get("resolved_inputs"),dict): raise Fault("fixture_contract_invalid")
        if doc.get("data_suitability") not in {"synthetic","deidentified","authorised_for_repository","unreviewed"}: raise Fault("fixture_contract_invalid")
        if not isinstance(doc.get("context"),dict) or set(doc["context"])-{"feedback","review_decisions"} or not isinstance(doc["context"].get("review_decisions",[]),list): raise Fault("fixture_contract_invalid")
        if not isinstance(doc.get("provenance"),dict) or set(doc["provenance"])-{"source_run_id","source_node_attempt"}: raise Fault("fixture_contract_invalid")
        origins=doc.get("origins")
        if not isinstance(origins,dict) or set(origins)-{"task_input","evidence","revision_feedback"} or not isinstance(origins.get("evidence",{}),dict) or not isinstance(origins.get("revision_feedback",{}),dict): raise Fault("fixture_contract_invalid")
        snaps=doc.get("evidence_snapshots")
        if not isinstance(snaps,list): raise Fault("fixture_contract_invalid")
        seen=set()
        for row in snaps:
            if not isinstance(row,dict) or set(row)!={"corpus_id","profile","sources","items"} or not isinstance(row["corpus_id"],str) or not row["corpus_id"] or row["corpus_id"] in seen or not isinstance(row["profile"],dict) or not isinstance(row["sources"],list) or not isinstance(row["items"],list): raise Fault("fixture_contract_invalid")
            seen.add(row["corpus_id"])
        if "captured_configuration" in doc and not isinstance(doc["captured_configuration"],dict): raise Fault("fixture_contract_invalid")
        return doc
    def assert_registered(self,doc):
        self.validate_envelope(doc)
        try: registered=self.registered(doc["id"],doc["version"])
        except Fault as exc:
            if exc.code=="fixture_not_found": raise Fault("recording_pair_mismatch") from None
            raise
        if encode(registered)!=encode(doc): raise Fault("fixture_version_changed")
        return registered

    def save_scratch(self,doc):
        self.validate_envelope(doc); fid=doc["id"]; ver=doc["version"]; path=self.scratch/self.filename(fid,ver)
        if path.exists() and encode(json.loads(path.read_text(encoding="utf-8")))!=encode(doc): raise Fault("fixture_version_changed")
        path.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8")
        self.store.set_fixture_record(fid,ver,{"id":fid,"version":ver,"workflow_id":doc["workflow_id"],"node_id":doc["node_id"],"kind":"scratch","path":str(path),"document":doc})
        return path
    def list(self):
        rows=self.store.fixture_records_all()
        known={(r["id"],r["version"]) for r in rows}
        for p in self.repo_root.glob("*/*/fixture-*.json"):
            try:
                doc=self.load(p); key=(doc["id"],doc["version"])
                if key not in known:
                    rows.append({"id":doc["id"],"version":doc["version"],"workflow_id":doc["workflow_id"],"node_id":doc["node_id"],"kind":"repository","path":str(p),"document":doc}); known.add(key)
            except (Fault,ValueError,KeyError):
                continue
        return rows
    def registered(self,fixture_id,version):
        row=self.store.fixture_record(validate_fixture_id(fixture_id),validate_version(version))
        if row: return deepcopy(row["document"])
        matches=[r for r in self.list() if r["id"]==fixture_id and r["version"]==version]
        if len(matches)!=1: raise Fault("fixture_not_found")
        return deepcopy(matches[0]["document"])
    def capture(self,run,node,attempt,origins=None):
        step=self.store.step_attempt(run["id"],node,attempt)
        if not step: raise Fault("step_attempt_not_found")
        snapshots=[]
        for cid in run.get("corpora",[]):
            c=self.store.corpus(cid); snapshots.append({"corpus_id":cid,"profile":c["profile"],"sources":c["sources"],"items":c["items"]})
        return {"fixture_schema_version":1,"id":"capture.placeholder","version":1,"workflow_id":run["snapshot"]["manifest"]["id"],"node_id":node,"step_contract_version":run["step_contract_version"],
                "resolved_inputs":deepcopy(step["resolved_inputs"]),"evidence_snapshots":snapshots,"context":{"feedback":run.get("feedback",{}).get(node),"review_decisions":[]},
                "provenance":{"source_run_id":run["id"],"source_node_attempt":attempt},"data_suitability":"unreviewed","origins":deepcopy(origins or run.get("origins",{})),"captured_configuration":deepcopy(step.get("node_config",{}))}
    def promote(self,source,fixture_id,version,suitability,acknowledge,actor,workflow,node):
        validate_actor(actor); validate_fixture_id(fixture_id); validate_version(version)
        if suitability not in {"synthetic","deidentified","authorised_for_repository"} or not acknowledge: raise Fault("fixture_review_required")
        doc=deepcopy(source); doc["id"]=fixture_id; doc["version"]=version; doc["data_suitability"]=suitability
        target=(self.repo_root/workflow/node).resolve(); root=self.repo_root.resolve(); target.mkdir(parents=True,exist_ok=True)
        if not target.is_relative_to(root): raise Fault("fixture_path_invalid")
        path=(target/self.filename(fixture_id,version)).resolve()
        if not path.is_relative_to(target) or path.exists(): raise Fault("fixture_version_changed")
        path.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8")
        self.store.set_fixture_record(fixture_id,version,{"id":fixture_id,"version":version,"workflow_id":workflow,"node_id":node,"kind":"repository","path":str(path),"document":doc,"actor":validate_actor(actor)})
        return path
    def load(self,path):
        doc=json.loads(Path(path).read_text(encoding="utf-8")); return self.validate_envelope(doc)
    def validate_tape(self,tape,fixture):
        allowed={"tape_schema_version","id","version","workflow_id","node_id","step_contract_version","fixture_ref","responses"}
        if not isinstance(tape,dict) or set(tape)!=allowed or tape.get("tape_schema_version")!=1:
            raise Fault("recording_pair_mismatch")
        validate_fixture_id(tape.get("id")); validate_version(tape.get("version"))
        ref=tape.get("fixture_ref",{})
        if ref!={"id":fixture["id"],"version":fixture["version"]} or tape.get("workflow_id")!=fixture["workflow_id"] or tape.get("node_id")!=fixture["node_id"] or tape.get("step_contract_version")!=fixture["step_contract_version"]: raise Fault("recording_pair_mismatch")
        seen=set()
        if not isinstance(tape.get("responses"),list): raise Fault("recording_pair_mismatch")
        for row in tape["responses"]:
            if not isinstance(row,dict) or set(row)!={"attempt","call_index","content"} or not isinstance(row.get("content"),str): raise Fault("recording_pair_mismatch")
            key=(row.get("attempt"),row.get("call_index"))
            if not all(isinstance(x,int) and not isinstance(x,bool) and x>0 for x in key) or key in seen: raise Fault("recording_pair_mismatch")
            seen.add(key)
        return tape
    def load_tape(self,path,fixture):
        tape=json.loads(Path(path).read_text(encoding="utf-8")); return self.validate_tape(tape,fixture)
    def delete_scratch(self,fixture_id,version):
        doc=self.registered(fixture_id,version); row=self.store.fixture_record(fixture_id,version)
        if not row or row.get("kind")!="scratch": raise Fault("fixture_delete_denied")
        path=Path(row["path"]).resolve()
        if not path.is_relative_to(self.scratch.resolve()): raise Fault("fixture_path_invalid")
        self.store.delete_fixture_record(fixture_id,version)
        try: path.unlink()
        except FileNotFoundError: pass
        return doc

