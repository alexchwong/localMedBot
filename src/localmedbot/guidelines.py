"""Independent guideline-set discovery, immutable releases, devel snapshots, promotion."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json,os,re,shutil,tempfile,time,uuid,yaml
from .contracts import Fault,validate_actor
from .knowledge import prepare_sources,indexes_valid,source_passage
from .storage import encode

_SET_RE=re.compile(r"[a-z0-9_-]{1,64}\Z")

class GuidelineRegistry:
    def __init__(self,root,store): self.root=Path(root).resolve(); self.store=store
    def _validate_snapshot(self,snap):
        if not isinstance(snap,dict) or snap.get("schema_version")!=1 or not isinstance(snap.get("corpus_uuid"),str) or not snap["corpus_uuid"] or not isinstance(snap.get("sources"),list) or not isinstance(snap.get("profile"),dict) or not isinstance(snap.get("items"),list): raise Fault("guideline_snapshot_invalid")
        source_ids={x.get("id") for x in snap["sources"] if isinstance(x,dict)}
        if len(source_ids)!=len(snap["sources"]) or None in source_ids: raise Fault("guideline_snapshot_invalid")
        seen=set(); corpus={"id":snap["corpus_uuid"],"sources":snap["sources"]}
        for item in snap["items"]:
            if not isinstance(item,dict) or not {"id","source","locator","text","indexes","assertion_kind"}<=set(item) or item["id"] in seen or item["source"] not in source_ids: raise Fault("guideline_snapshot_invalid")
            seen.add(item["id"]); indexes_valid(item["indexes"],snap["profile"].get("indexes",{}))
            try: source_passage(corpus,item)
            except (KeyError,IndexError,ValueError,TypeError): raise Fault("guideline_snapshot_invalid") from None
        return snap
    def _dir(self,set_id):
        if not _SET_RE.fullmatch(set_id): raise Fault("guideline_set_id")
        p=(self.root/set_id).resolve()
        if not p.is_relative_to(self.root) or not p.is_dir(): raise Fault("guideline_set_not_found")
        return p
    def _manifest(self,set_id):
        p=self._dir(set_id)/"manifest.yaml"; doc=yaml.safe_load(p.read_text(encoding="utf-8"))
        if doc.get("id")!=set_id: raise Fault("guideline_manifest_invalid")
        return doc
    def list(self,developer=False):
        rows=[]
        for p in sorted(self.root.glob("*/manifest.yaml")):
            m=yaml.safe_load(p.read_text(encoding="utf-8")); row={"id":m["id"],"name":m["name"],"default_release":m["default_release"],"releases":list(m.get("releases",[])),"content_origin":m.get("content_origin","unknown")}
            if developer: row["devel_available"]=bool(m.get("devel_available"))
            rows.append(row)
        return rows
    def _load_snapshot(self,set_id,selector):
        base=self._dir(set_id); m=self._manifest(set_id); release=m["default_release"] if selector=="default" else selector
        if release=="devel":
            if not m.get("devel_available"): raise Fault("development_unavailable")
            folder=base/"devel"
        else:
            if release not in m.get("releases",[]): raise Fault("guideline_release_not_found")
            folder=base/"releases"/release
        snap=json.loads((folder/"snapshot.json").read_text(encoding="utf-8")); sources=json.loads((folder/"sources.json").read_text(encoding="utf-8")); profile=yaml.safe_load((folder/"ingestion.yaml").read_text(encoding="utf-8"))
        if snap.get("set_id")!=set_id or snap.get("release_id")!=("devel" if release=="devel" else release) or snap.get("sources")!=sources or snap.get("profile")!=profile: raise Fault("immutable_release_changed")
        self._validate_snapshot(snap); return release,snap
    def resolve(self,set_id,selector,developer=False):
        if selector=="devel" and not developer: raise Fault("developer_disabled")
        release,snap=self._load_snapshot(set_id,selector); cid=snap["corpus_uuid"]
        self.store.publish("guideline",set_id,snap["profile"],snap["sources"],snap["items"],activate=False,cid=cid)
        self.store.set_guideline_set(set_id,{"set_id":set_id,"default_release":self._manifest(set_id)["default_release"],"content_origin":self._manifest(set_id).get("content_origin","unknown")})
        self.store.set_guideline_release(set_id,release,{"corpus_id":cid,"snapshot":snap})
        return {"set_id":set_id,"selector":selector,"resolved_release":release,"corpus_id":cid,"content_origin":self._manifest(set_id).get("content_origin","unknown")}
    def devel_profile(self,set_id):
        base=self._dir(set_id); m=self._manifest(set_id)
        if not m.get("devel_available"): raise Fault("development_unavailable")
        return yaml.safe_load((base/"devel"/"ingestion.yaml").read_text(encoding="utf-8"))
    def publish_devel(self,set_id,sources,profile,items,provenance=None):
        base=self._dir(set_id); m=self._manifest(set_id)
        if not m.get("devel_available"): raise Fault("development_unavailable")
        folder=base/"devel"; cid=uuid.uuid4().hex
        snap={"schema_version":1,"corpus_uuid":cid,"set_id":set_id,"release_id":"devel","sources":deepcopy(sources),"profile":deepcopy(profile),"items":deepcopy(items),"provenance":deepcopy(provenance or {"mode":"direct","created":time.time()})}
        tmp=Path(tempfile.mkdtemp(prefix="devel-stage-",dir=base)); previous=None
        try:
            (tmp/"sources.json").write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding="utf-8")
            (tmp/"ingestion.yaml").write_text(yaml.safe_dump(profile,sort_keys=False),encoding="utf-8")
            (tmp/"snapshot.json").write_text(json.dumps(snap,ensure_ascii=False,indent=2),encoding="utf-8")
            previous=base/(".devel-previous-"+uuid.uuid4().hex)
            if folder.exists(): os.replace(folder,previous)
            try: os.replace(tmp,folder)
            except Exception:
                if previous.exists() and not folder.exists(): os.replace(previous,folder)
                raise
            if previous.exists(): shutil.rmtree(previous)
        finally:
            if tmp.exists(): shutil.rmtree(tmp,ignore_errors=True)
            if previous is not None and previous.exists() and folder.exists(): shutil.rmtree(previous,ignore_errors=True)
        return cid
    def import_devel(self,set_id,sources,profile=None):
        profile=profile or self.devel_profile(set_id)
        if profile.get("mode")=="model": raise Fault("model_required")
        items=prepare_sources(sources,profile)
        return self.publish_devel(set_id,sources,profile,items,{"mode":"direct","created":time.time()})
    def promote(self,set_id,expected_snapshot,note,actor):
        actor=validate_actor(actor)
        if not isinstance(note,str) or not note.strip(): raise Fault("promotion_note_required")
        base=self._dir(set_id); m=self._manifest(set_id); _,dev=self._load_snapshot(set_id,"devel")
        if dev["corpus_uuid"]!=expected_snapshot: raise Fault("stale_development_snapshot")
        _,current=self._load_snapshot(set_id,m["default_release"])
        if encode({"sources":dev["sources"],"profile":dev["profile"],"items":dev["items"]})==encode({"sources":current["sources"],"profile":current["profile"],"items":current["items"]}): raise Fault("no_development_change")
        nums=[int(x[1:]) for x in m.get("releases",[]) if re.fullmatch(r"v\d+",x)]; release=f"v{max(nums or [0])+1}"; dest=base/"releases"/release
        if dest.exists(): raise Fault("immutable_release_changed")
        staging=Path(tempfile.mkdtemp(prefix="promote-",dir=base)); release_dir=staging/release; release_dir.mkdir()
        snap={**deepcopy(dev),"release_id":release,"provenance":{**dev.get("provenance",{}),"promoted_at":time.time(),"actor":actor,"note":note.strip(),"from_snapshot":dev["corpus_uuid"]}}
        (release_dir/"sources.json").write_text(json.dumps(dev["sources"],ensure_ascii=False,indent=2),encoding="utf-8"); (release_dir/"ingestion.yaml").write_text(yaml.safe_dump(dev["profile"],sort_keys=False),encoding="utf-8"); (release_dir/"snapshot.json").write_text(json.dumps(snap,ensure_ascii=False,indent=2),encoding="utf-8")
        dest.parent.mkdir(exist_ok=True); os.replace(release_dir,dest)
        old=m["default_release"]
        # Prepare a fresh editable development generation from the already validated
        # frozen items. This never re-runs model-assisted ingestion.
        new_devel=self.publish_devel(set_id,deepcopy(dev["sources"]),deepcopy(dev["profile"]),deepcopy(dev["items"]),{"mode":"promotion_copy","created":time.time(),"from_release":release})
        m["default_release"]=release; m.setdefault("releases",[]).append(release)
        mt=base/"manifest.yaml.tmp"; mt.write_text(yaml.safe_dump(m,sort_keys=False),encoding="utf-8"); os.replace(mt,base/"manifest.yaml")
        self.store.set_guideline_release(set_id,release,{"corpus_id":snap["corpus_uuid"],"snapshot":snap})
        self.store.set_guideline_set(set_id,{"set_id":set_id,"default_release":release,"content_origin":m.get("content_origin","unknown")})
        shutil.rmtree(staging,ignore_errors=True)
        return {"set_id":set_id,"old_default":old,"new_default":release,"snapshot_id":snap["corpus_uuid"],"new_development_snapshot":new_devel,"actor":actor,"note":note.strip()}
