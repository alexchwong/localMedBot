"""SQLite execution state plus staged immutable per-run files."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json, os, sqlite3, time, uuid, shutil, hashlib
from .contracts import Fault
from .errors import present_error
from . import STORAGE_SCHEMA_VERSION


def uid(): return uuid.uuid4().hex

def encode(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def _fsync_dir(path: Path):
    if os.name=="nt": return
    try:
        fd=os.open(str(path),os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass


def _atomic_replace(path: Path, text: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f".tmp-{uid()}")
    with tmp.open("w",encoding="utf-8") as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path); _fsync_dir(path.parent)


def _immutable_write(path: Path, text: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists(): raise Fault("storage_conflict",str(path))
    tmp=path.with_name(path.name+f".tmp-{uid()}")
    try:
        with tmp.open("x",encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        if path.exists(): raise Fault("storage_conflict",str(path))
        os.rename(tmp,path); _fsync_dir(path.parent)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass


class WriterGuard:
    def __init__(self, root: Path, enabled: bool):
        self.enabled=enabled; self.handle=None
        if not enabled: return
        root.mkdir(parents=True,exist_ok=True)
        path=root/"writer.lock"; path.touch(exist_ok=True)
        self.handle=path.open("a+b")
        try:
            if os.name=="nt":
                import msvcrt; msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl; fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close(); self.handle=None
            raise Fault("data_directory_busy") from exc
    def close(self):
        if not self.handle: return
        try:
            if os.name=="nt":
                import msvcrt; self.handle.seek(0); msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl; fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
        finally:
            self.handle.close(); self.handle=None


class Store:
    def __init__(self, root, writer=True, runs_root=None):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.runs_root=Path(runs_root).resolve() if runs_root is not None else (self.root/"runs").resolve()
        if writer: self.runs_root.mkdir(parents=True,exist_ok=True)
        self.guard=WriterGuard(self.root,writer)
        self.dbpath=self.root/"state.sqlite"; self.writer=writer
        existed=self.dbpath.exists(); version=None; legacy_existing=False
        if existed:
            with self.db() as db:
                has_meta=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
                if has_meta: version=db.execute("SELECT value FROM meta WHERE key='storage_schema_version'").fetchone()
                else: legacy_existing=True
        if writer and legacy_existing:
            backup=self.root/("state.pre-v1.%d.sqlite"%int(time.time()))
            src=sqlite3.connect(self.dbpath); dst=sqlite3.connect(backup)
            try: src.backup(dst)
            finally: src.close(); dst.close()
        if writer or not existed: self._migrate(version)
        if writer: self.recover_startup()

    def close(self): self.guard.close()

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.dbpath,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _migrate(self, existing):
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(run TEXT,node TEXT,revision INTEGER,path TEXT,metadata TEXT,PRIMARY KEY(run,node,revision));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run TEXT,time REAL,kind TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS corpora(id TEXT PRIMARY KEY,scope TEXT,name TEXT,profile TEXT,sources TEXT,items TEXT);
            CREATE TABLE IF NOT EXISTS active_corpora(scope TEXT,name TEXT,id TEXT,PRIMARY KEY(scope,name));
            CREATE TABLE IF NOT EXISTS profile_overlays(profile_id TEXT PRIMARY KEY,document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS step_attempts(run_id TEXT,node_id TEXT,attempt INTEGER,document TEXT NOT NULL,PRIMARY KEY(run_id,node_id,attempt));
            CREATE TABLE IF NOT EXISTS model_calls(request_id TEXT PRIMARY KEY,run_id TEXT,node_id TEXT,attempt INTEGER,call_index INTEGER,document TEXT NOT NULL, UNIQUE(run_id,node_id,attempt,call_index));
            CREATE TABLE IF NOT EXISTS tool_calls(run_id TEXT,node_id TEXT,attempt INTEGER,tool_index INTEGER,document TEXT NOT NULL, PRIMARY KEY(run_id,node_id,attempt,tool_index));
            CREATE TABLE IF NOT EXISTS review_events(run_id TEXT,request_id TEXT,document TEXT NOT NULL,PRIMARY KEY(run_id,request_id));
            CREATE TABLE IF NOT EXISTS guideline_sets(set_id TEXT PRIMARY KEY,document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guideline_releases(set_id TEXT,release_id TEXT,document TEXT NOT NULL,PRIMARY KEY(set_id,release_id));
            CREATE TABLE IF NOT EXISTS fixture_records(fixture_id TEXT,version INTEGER,document TEXT NOT NULL,PRIMARY KEY(fixture_id,version));
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY,run_id TEXT,node_id TEXT,attempt INTEGER,document TEXT NOT NULL,UNIQUE(run_id,node_id,attempt));
            CREATE TABLE IF NOT EXISTS physical_calls(call_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,run_id TEXT NOT NULL,node_id TEXT NOT NULL,attempt INTEGER NOT NULL,transport_ordinal INTEGER NOT NULL,document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS repairs(repair_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT NOT NULL,attempt INTEGER NOT NULL,operation_id TEXT NOT NULL,document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_revisions(link_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,checker_id TEXT NOT NULL,target_id TEXT NOT NULL,document TEXT NOT NULL);
            ''')
            cols={r[1] for r in db.execute("PRAGMA table_info(runs)")}
            if "inspection_revision" not in cols:
                db.execute("ALTER TABLE runs ADD COLUMN inspection_revision INTEGER NOT NULL DEFAULT 0")
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('storage_schema_version',?)",(str(STORAGE_SCHEMA_VERSION),))

    def _bump(self,db,rid,run=None):
        row=db.execute("SELECT document,inspection_revision FROM runs WHERE id=?",(rid,)).fetchone()
        if not row:
            if run is None: return 0
            rev=int(run.get("inspection_revision",0))+1; run["inspection_revision"]=rev
            db.execute("INSERT INTO runs(id,document,inspection_revision) VALUES(?,?,?)",(rid,encode(run),rev)); return rev
        current=int(row[1] or 0); rev=current+1
        doc=run if run is not None else json.loads(row[0]); doc["inspection_revision"]=rev
        db.execute("UPDATE runs SET document=?,inspection_revision=? WHERE id=?",(encode(doc),rev,rid))
        if run is not None: run["inspection_revision"]=rev
        return rev

    def run_dir(self,rid): return self.runs_root/rid

    def create_run(self,run):
        folder=self.run_dir(run["id"])
        if folder.exists(): raise Fault("run_id_conflict",run["id"])
        folder.mkdir(parents=True)
        for rel in ("run-config","artifacts","attempts","logs"):
            (folder/rel).mkdir()
        _immutable_write(folder/"input.json",json.dumps(run["input"],ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        frozen={"snapshot":run["snapshot"],"profile":run["profile"],"retry_limits":run.get("retry_limits"),"runtime_version":run.get("runtime_version"),"run_contract_version":run.get("run_contract_version"),"source_version":run.get("snapshot",{}).get("source_version")}
        _immutable_write(folder/"run-config"/"resolved.json",json.dumps(frozen,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        run["run_folder"]=str(folder); run["files"]={"input":"input.json","run_config":"run-config/resolved.json"}
        with self.db() as db: self._bump(db,run["id"],run)
        self.rebuild_derived(run["id"])

    def put_run(self,run):
        with self.db() as db: self._bump(db,run["id"],run)
        if self.run_dir(run["id"]).exists(): self.rebuild_derived(run["id"])

    def run(self,rid):
        with self.db() as db: row=db.execute("SELECT document,inspection_revision FROM runs WHERE id=?",(rid,)).fetchone()
        if not row: raise Fault("run_not_found")
        value=json.loads(row[0]); value["inspection_revision"]=int(row[1] or value.get("inspection_revision",0)); value.setdefault("legacy", "run_contract_version" not in value)
        return value

    def runs(self):
        with self.db() as db: rows=db.execute("SELECT document,inspection_revision FROM runs ORDER BY rowid DESC").fetchall()
        out=[]
        for r in rows:
            d=json.loads(r[0]); d["inspection_revision"]=int(r[1]); out.append(d)
        return out

    def event(self,rid,kind,data):
        with self.db() as db:
            db.execute("INSERT INTO events(run,time,kind,data) VALUES(?,?,?,?)",(rid,time.time(),kind,encode(data))); self._bump(db,rid)

    def events(self,rid):
        with self.db() as db: rows=db.execute("SELECT id,time,kind,data FROM events WHERE run=? ORDER BY id",(rid,)).fetchall()
        return [{"id":r[0],"time":r[1],"kind":r[2],"data":json.loads(r[3])} for r in rows]

    def next_revision(self,rid,node):
        with self.db() as db: return db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM artifacts WHERE run=? AND node=?",(rid,node)).fetchone()[0]

    def commit(self,run,node,payload,metadata):
        rev=self.next_revision(run["id"],node)
        rel=Path("artifacts")/node/f"{rev:04d}_{uid()}.json"; path=self.run_dir(run["id"])/rel
        _immutable_write(path,json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        run["active"][node]=rev; run["nodes"][node]="complete"
        md={**metadata,"run_id":run["id"],"runtime_version":run.get("runtime_version"),"application_version":run["snapshot"]["manifest"].get("version")}
        with self.db() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",(run["id"],node,rev,str(rel),encode(md)))
            self._bump(db,run["id"],run)
        self.rebuild_derived(run["id"]); return rev

    def artifact(self,rid,node,revision=None):
        if revision is None: revision=self.run(rid)["active"].get(node)
        with self.db() as db: row=db.execute("SELECT path,metadata FROM artifacts WHERE run=? AND node=? AND revision=?",(rid,node,revision)).fetchone()
        if not row: raise Fault("artifact_not_found",node)
        path=self.run_dir(rid)/row[0]
        try: payload=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): raise Fault("storage_integrity_failure",str(path)) from None
        return {"id":node,"revision":revision,"payload":payload,"metadata":json.loads(row[1]),"path":str(path)}

    def _attempt_dir(self,rid,node,attempt):
        return self.run_dir(rid)/"attempts"/node/f"{int(attempt):04d}"

    def put_step_attempt(self,rid,node,attempt,doc):
        d=self._attempt_dir(rid,node,attempt); d.mkdir(parents=True,exist_ok=True)
        rp=d/"resolved-input.json"
        if not rp.exists() and "resolved_inputs" in doc:
            _immutable_write(rp,json.dumps(doc["resolved_inputs"],ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        _atomic_replace(d/"attempt.json",json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO step_attempts VALUES(?,?,?,?)",(rid,node,attempt,encode(doc))); self._bump(db,rid)

    def step_attempt(self,rid,node,attempt):
        with self.db() as db: row=db.execute("SELECT document FROM step_attempts WHERE run_id=? AND node_id=? AND attempt=?",(rid,node,attempt)).fetchone()
        return json.loads(row[0]) if row else None

    def put_operation(self,doc):
        with self.db() as db:
            row=db.execute("SELECT document FROM operations WHERE run_id=? AND node_id=? AND attempt=?",(doc["run_id"],doc["node_id"],doc["attempt"])).fetchone()
            if row:
                old=json.loads(row[0])
                if old.get("operation_id")!=doc.get("operation_id"): return old
                return old
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)",(doc["operation_id"],doc["run_id"],doc["node_id"],doc["attempt"],encode(doc))); self._bump(db,doc["run_id"])
        return doc

    def update_operation(self,operation_id,doc):
        with self.db() as db:
            row=db.execute("SELECT run_id FROM operations WHERE operation_id=?",(operation_id,)).fetchone()
            if not row: raise Fault("operation_not_found")
            db.execute("UPDATE operations SET document=? WHERE operation_id=?",(encode(doc),operation_id)); self._bump(db,row[0])

    def operation_for_attempt(self,rid,node,attempt):
        with self.db() as db: row=db.execute("SELECT document FROM operations WHERE run_id=? AND node_id=? AND attempt=?",(rid,node,attempt)).fetchone()
        return json.loads(row[0]) if row else None

    def operations(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM operations WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _call_dir(self,doc):
        return self._attempt_dir(doc["run_id"],doc["node_id"],doc["attempt"])/"calls"/doc["request_id"]

    def put_model_call(self,doc):
        d=self._call_dir(doc); d.mkdir(parents=True,exist_ok=True)
        request_file=d/"request.json"
        if not request_file.exists():
            persisted={k:v for k,v in doc.items() if k not in {"raw_response","parsed","response_metadata","status","findings","error"}}
            _immutable_write(request_file,json.dumps(persisted,ensure_ascii=False,indent=2,sort_keys=True)+"\n"); doc["request_path"]=str(request_file.relative_to(self.run_dir(doc["run_id"])))
        with self.db() as db:
            try:
                db.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?)",(doc["request_id"],doc["run_id"],doc["node_id"],doc["attempt"],doc["call_index"],encode(doc))); self._bump(db,doc["run_id"])
            except sqlite3.IntegrityError:
                row=db.execute("SELECT document FROM model_calls WHERE run_id=? AND node_id=? AND attempt=? AND call_index=?",(doc["run_id"],doc["node_id"],doc["attempt"],doc["call_index"])).fetchone(); old=json.loads(row[0])
                if old.get("canonical")!=doc.get("canonical"): raise Fault("resume_contract_mismatch")
                return old
        return doc

    def update_model_call(self,request_id,doc):
        old=self.model_call(request_id)
        if old.get("status") in {"complete","invalid","cancelled"} and encode(old)!=encode(doc): raise Fault("immutable_call_changed",request_id)
        if doc.get("raw_response") is not None and not doc.get("response_path"):
            d=self._call_dir(doc); path=d/"response.txt"; _immutable_write(path,doc["raw_response"]); doc["response_path"]=str(path.relative_to(self.run_dir(doc["run_id"])))
        with self.db() as db:
            db.execute("UPDATE model_calls SET document=? WHERE request_id=?",(encode(doc),request_id)); self._bump(db,doc["run_id"])

    def model_call(self,request_id):
        with self.db() as db: row=db.execute("SELECT document FROM model_calls WHERE request_id=?",(request_id,)).fetchone()
        if not row: raise Fault("handoff_not_found")
        return json.loads(row[0])

    def model_call_at(self,rid,node,attempt,index):
        with self.db() as db: row=db.execute("SELECT document FROM model_calls WHERE run_id=? AND node_id=? AND attempt=? AND call_index=?",(rid,node,attempt,index)).fetchone()
        return json.loads(row[0]) if row else None

    def model_calls_for_turn(self,operation_id,turn_index):
        with self.db() as db:
            rows=db.execute("SELECT document FROM model_calls WHERE json_extract(document,'$.operation_id')=? AND json_extract(document,'$.turn_index')=? ORDER BY call_index",(operation_id,turn_index)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_physical_call(self,doc):
        with self.db() as db:
            try:
                db.execute("INSERT INTO physical_calls VALUES(?,?,?,?,?,?,?)",(doc["call_id"],doc["request_id"],doc["run_id"],doc["node_id"],doc["attempt"],doc["transport_ordinal"],encode(doc))); self._bump(db,doc["run_id"])
            except sqlite3.IntegrityError:
                row=db.execute("SELECT document FROM physical_calls WHERE call_id=?",(doc["call_id"],)).fetchone(); return json.loads(row[0])
        return doc

    def update_physical_call(self,call_id,doc):
        with self.db() as db:
            row=db.execute("SELECT run_id FROM physical_calls WHERE call_id=?",(call_id,)).fetchone()
            if not row: raise Fault("physical_call_not_found")
            db.execute("UPDATE physical_calls SET document=? WHERE call_id=?",(encode(doc),call_id)); self._bump(db,row[0])

    def physical_calls(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM physical_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def physical_calls_for_request(self,request_id):
        with self.db() as db: rows=db.execute("SELECT document FROM physical_calls WHERE request_id=? ORDER BY rowid",(request_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def physical_call(self,call_id):
        with self.db() as db: row=db.execute("SELECT document FROM physical_calls WHERE call_id=?",(call_id,)).fetchone()
        if not row: raise Fault("physical_call_not_found")
        return json.loads(row[0])

    def put_repair(self,doc):
        d=self._attempt_dir(doc["run_id"],doc["node_id"],doc["attempt"])/"repairs"; d.mkdir(parents=True,exist_ok=True)
        path=d/f"{doc['repair_id']}.json"
        if not path.exists(): _immutable_write(path,json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        doc.setdefault("path",str(path.relative_to(self.run_dir(doc["run_id"]))))
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO repairs VALUES(?,?,?,?,?,?)",(doc["repair_id"],doc["run_id"],doc["node_id"],doc["attempt"],doc["operation_id"],encode(doc))); self._bump(db,doc["run_id"])
        return self.repair(doc["repair_id"])

    def update_repair(self,repair_id,doc):
        with self.db() as db:
            row=db.execute("SELECT run_id FROM repairs WHERE repair_id=?",(repair_id,)).fetchone()
            if not row: raise Fault("repair_not_found")
            db.execute("UPDATE repairs SET document=? WHERE repair_id=?",(encode(doc),repair_id)); self._bump(db,row[0])

    def repair(self,repair_id):
        with self.db() as db: row=db.execute("SELECT document FROM repairs WHERE repair_id=?",(repair_id,)).fetchone()
        if not row: raise Fault("repair_not_found")
        return json.loads(row[0])

    def repairs(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM repairs WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def repairs_for_operation(self,operation_id):
        with self.db() as db: rows=db.execute("SELECT document FROM repairs WHERE operation_id=? ORDER BY rowid",(operation_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_semantic_revision(self,doc):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO semantic_revisions VALUES(?,?,?,?,?)",(doc["link_id"],doc["run_id"],doc["checker_id"],doc["target_id"],encode(doc))); self._bump(db,doc["run_id"])
        return doc

    def update_semantic_revision(self,link_id,doc):
        with self.db() as db:
            row=db.execute("SELECT run_id FROM semantic_revisions WHERE link_id=?",(link_id,)).fetchone()
            if not row: raise Fault("semantic_revision_not_found")
            db.execute("UPDATE semantic_revisions SET document=? WHERE link_id=?",(encode(doc),link_id)); self._bump(db,row[0])

    def semantic_revisions(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM semantic_revisions WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_tool_call(self,rid,node,attempt,index,doc):
        with self.db() as db:
            row=db.execute("SELECT document FROM tool_calls WHERE run_id=? AND node_id=? AND attempt=? AND tool_index=?",(rid,node,attempt,index)).fetchone()
            if row:
                old=json.loads(row[0])
                if old.get("canonical")!=doc.get("canonical"): raise Fault("resume_contract_mismatch")
                return old
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?)",(rid,node,attempt,index,encode(doc))); self._bump(db,rid)
        return doc

    def update_tool_call(self,rid,node,attempt,index,doc):
        with self.db() as db: db.execute("UPDATE tool_calls SET document=? WHERE run_id=? AND node_id=? AND attempt=? AND tool_index=?",(encode(doc),rid,node,attempt,index)); self._bump(db,rid)

    def step_attempts(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM step_attempts WHERE run_id=? ORDER BY node_id,attempt",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def model_calls(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM model_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def tool_calls(self,rid):
        with self.db() as db: rows=db.execute("SELECT document FROM tool_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def review_event(self,rid,request_id):
        with self.db() as db: row=db.execute("SELECT document FROM review_events WHERE run_id=? AND request_id=?",(rid,request_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put_review_event(self,rid,request_id,doc):
        with self.db() as db: db.execute("INSERT INTO review_events VALUES(?,?,?)",(rid,request_id,encode(doc))); self._bump(db,rid)

    def corpus(self,cid):
        with self.db() as db: row=db.execute("SELECT * FROM corpora WHERE id=?",(cid,)).fetchone()
        if not row: raise Fault("corpus_not_found")
        return {k:json.loads(row[k]) if k in {"profile","sources","items"} else row[k] for k in row.keys()}

    def publish(self,scope,name,profile,sources,items,activate=True,cid=None):
        ep,es,ei=encode(profile),encode(sources),encode(items)
        with self.db() as db:
            row=db.execute("SELECT id FROM corpora WHERE scope=? AND name=? AND profile=? AND sources=? AND items=?",(scope,name,ep,es,ei)).fetchone(); actual=row[0] if row else (cid or uid())
            if row is None:
                existing=db.execute("SELECT profile,sources,items FROM corpora WHERE id=?",(actual,)).fetchone()
                if existing and tuple(existing)!=(ep,es,ei): raise Fault("immutable_release_changed")
                if not existing: db.execute("INSERT INTO corpora VALUES(?,?,?,?,?,?)",(actual,scope,name,ep,es,ei))
            if activate: db.execute("INSERT OR REPLACE INTO active_corpora VALUES(?,?,?)",(scope,name,actual))
            if db.execute("SELECT 1 FROM runs WHERE id=?",(scope,)).fetchone(): self._bump(db,scope)
        return actual

    def active_corpus(self,scope,name):
        with self.db() as db: row=db.execute("SELECT id FROM active_corpora WHERE scope=? AND name=?",(scope,name)).fetchone()
        return row[0] if row else None

    def preference(self,key,default=None):
        with self.db() as db: row=db.execute("SELECT value FROM preferences WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_preference(self,key,value):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)",(key,encode(value)))

    def profile_overlay(self,pid):
        with self.db() as db: row=db.execute("SELECT document FROM profile_overlays WHERE profile_id=?",(pid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_profile_overlay(self,pid,doc):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO profile_overlays VALUES(?,?)",(pid,encode(doc)))

    def set_guideline_set(self,set_id,doc):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO guideline_sets VALUES(?,?)",(set_id,encode(doc)))
    def guideline_set(self,set_id):
        with self.db() as db: row=db.execute("SELECT document FROM guideline_sets WHERE set_id=?",(set_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def guideline_sets(self):
        with self.db() as db: return [json.loads(r[0]) for r in db.execute("SELECT document FROM guideline_sets ORDER BY set_id")]
    def set_guideline_release(self,set_id,release_id,doc):
        with self.db() as db:
            row=db.execute("SELECT document FROM guideline_releases WHERE set_id=? AND release_id=?",(set_id,release_id)).fetchone()
            if row and encode(json.loads(row[0]))!=encode(doc): raise Fault("immutable_release_changed")
            db.execute("INSERT OR IGNORE INTO guideline_releases VALUES(?,?,?)",(set_id,release_id,encode(doc)))
    def guideline_release(self,set_id,release_id):
        with self.db() as db: row=db.execute("SELECT document FROM guideline_releases WHERE set_id=? AND release_id=?",(set_id,release_id)).fetchone()
        return json.loads(row[0]) if row else None

    def set_fixture_record(self,fixture_id,version,doc):
        with self.db() as db:
            row=db.execute("SELECT document FROM fixture_records WHERE fixture_id=? AND version=?",(fixture_id,version)).fetchone()
            if row and encode(json.loads(row[0]))!=encode(doc): raise Fault("fixture_version_changed")
            db.execute("INSERT OR IGNORE INTO fixture_records VALUES(?,?,?)",(fixture_id,version,encode(doc)))
    def fixture_record(self,fixture_id,version):
        with self.db() as db: row=db.execute("SELECT document FROM fixture_records WHERE fixture_id=? AND version=?",(fixture_id,version)).fetchone()
        return json.loads(row[0]) if row else None
    def fixture_records_all(self):
        with self.db() as db: rows=db.execute("SELECT document FROM fixture_records ORDER BY fixture_id,version").fetchall()
        return [json.loads(r[0]) for r in rows]
    def delete_fixture_record(self,fixture_id,version):
        with self.db() as db: db.execute("DELETE FROM fixture_records WHERE fixture_id=? AND version=?",(fixture_id,version))

    def inspection_snapshot(self,rid):
        # All mutable/indexed state is selected under one SQLite read transaction.
        db=sqlite3.connect(self.dbpath,timeout=10); db.row_factory=sqlite3.Row
        try:
            db.execute("BEGIN")
            rr=db.execute("SELECT document,inspection_revision FROM runs WHERE id=?",(rid,)).fetchone()
            if not rr: raise Fault("run_not_found")
            run=json.loads(rr[0]); run["inspection_revision"]=int(rr[1]); run.setdefault("legacy","run_contract_version" not in run)
            ars=db.execute("SELECT node,revision,path,metadata FROM artifacts WHERE run=? ORDER BY node,revision",(rid,)).fetchall()
            events=db.execute("SELECT id,time,kind,data FROM events WHERE run=? ORDER BY id",(rid,)).fetchall()
            attempts=db.execute("SELECT document FROM step_attempts WHERE run_id=? ORDER BY node_id,attempt",(rid,)).fetchall()
            requests=db.execute("SELECT document FROM model_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            tools=db.execute("SELECT document FROM tool_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            physical=db.execute("SELECT document FROM physical_calls WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            repairs=db.execute("SELECT document FROM repairs WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            semantic=db.execute("SELECT document FROM semantic_revisions WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            ops=db.execute("SELECT document FROM operations WHERE run_id=? ORDER BY rowid",(rid,)).fetchall()
            db.commit()
        finally: db.close()
        all_artifacts=[]; active={}
        for row in ars:
            path=self.run_dir(rid)/row[2]
            try: payload=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,ValueError): raise Fault("storage_integrity_failure",str(path)) from None
            item={"id":row[0],"revision":row[1],"payload":payload,"metadata":json.loads(row[3]),"path":str(path)}; all_artifacts.append(item)
            if run.get("active",{}).get(row[0])==row[1]: active[row[0]]=item
        return {"run_id":rid,"inspection_revision":run["inspection_revision"],"run":run,"artifacts":active,"artifact_history":all_artifacts,"events":[{"id":r[0],"time":r[1],"kind":r[2],"data":json.loads(r[3])} for r in events],"step_attempts":[json.loads(r[0]) for r in attempts],"model_calls":[json.loads(r[0]) for r in requests],"physical_calls":[json.loads(r[0]) for r in physical],"repairs":[json.loads(r[0]) for r in repairs],"semantic_revisions":[json.loads(r[0]) for r in semantic],"operations":[json.loads(r[0]) for r in ops],"tool_calls":[json.loads(r[0]) for r in tools]}

    def rebuild_derived(self,rid):
        folder=self.run_dir(rid)
        if not folder.exists(): return
        try: run=self.run(rid)
        except Fault: return
        manifest={"run_id":rid,"status":run.get("status"),"created":run.get("created"),"application":run.get("snapshot",{}).get("manifest",{}).get("id"),"runtime_version":run.get("runtime_version"),"inspection_revision":run.get("inspection_revision"),"active":run.get("active",{}),"run_folder":str(folder),"approval":run.get("approval"),"review_disposition":run.get("review_disposition")}
        _atomic_replace(folder/"manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        output_node=run.get("snapshot",{}).get("workflow",{}).get("output")
        if output_node and run.get("active",{}).get(output_node):
            try:
                art=self.artifact(rid,output_node,run["active"][output_node]); text=art["payload"].get("text") if isinstance(art["payload"],dict) else None
                if isinstance(text,str): _atomic_replace(folder/"output.md",text.rstrip()+"\n")
            except Fault: pass
        try:
            from .usage import aggregate_usage
            summary=aggregate_usage(self.model_calls(rid),self.physical_calls(rid),self.semantic_revisions(rid))
            _atomic_replace(folder/"logs"/"model-usage.json",json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
        except Exception:
            pass

    def recover_startup(self):
        # Remove only uniquely named incomplete staging files. Never promote an orphan.
        for root in (self.root,self.runs_root):
            if not root.exists(): continue
            for p in root.rglob("*.tmp-*"):
                try: p.unlink()
                except OSError: pass
        # Verify every authoritative committed file reference. Missing/unreadable files
        # block only the affected run and are never reconstructed from another view.
        bad=[]
        with self.db() as db:
            artifact_rows=db.execute("SELECT run,node,revision,path FROM artifacts").fetchall()
            request_rows=db.execute("SELECT run_id,node_id,attempt,document FROM model_calls").fetchall()
            repair_rows=db.execute("SELECT run_id,node_id,attempt,document FROM repairs").fetchall()
            run_rows=db.execute("SELECT id FROM runs").fetchall()

        def verify(run_id,rel,*,node=None,attempt=None,revision=None,kind="file"):
            path=self.run_dir(run_id)/rel
            try:
                if not path.is_file(): raise OSError("missing")
                # Open and consume the bytes to catch permissions/read errors. JSON validity is
                # checked by the normal reader for structured records; storage integrity here is
                # about the committed payload being present and readable.
                with path.open("rb") as handle:
                    while handle.read(1024*1024): pass
            except OSError:
                bad.append({"run_id":run_id,"node":node,"attempt":attempt,"revision":revision,"path":str(rel),"kind":kind})

        for r in run_rows:
            verify(r[0],"input.json",kind="run_input")
            verify(r[0],"run-config/resolved.json",kind="run_config")
        for r in artifact_rows:
            verify(r[0],r[3],node=r[1],revision=r[2],kind="artifact")
        for r in request_rows:
            doc=json.loads(r[3])
            if doc.get("request_path"):
                verify(r[0],doc["request_path"],node=r[1],attempt=r[2],kind="model_request")
            if doc.get("response_path"):
                verify(r[0],doc["response_path"],node=r[1],attempt=r[2],kind="model_response")
        for r in repair_rows:
            doc=json.loads(r[3])
            if doc.get("path"):
                verify(r[0],doc["path"],node=r[1],attempt=r[2],kind="repair")
        if bad:
            _atomic_replace(self.root/"startup-storage-errors.json",json.dumps({"errors":bad},indent=2)+"\n")
            with self.db() as db:
                for item in bad:
                    row=db.execute("SELECT document FROM runs WHERE id=?",(item["run_id"],)).fetchone()
                    if not row: continue
                    run=json.loads(row[0]); run["status"]="blocked"
                    attempt=item.get("attempt")
                    if attempt is None and item.get("node") is not None: attempt=run.get("attempts",{}).get(item["node"])
                    run["error"]=present_error({"code":"storage_integrity_failure","detail":item["path"]},stage=item.get("node"),attempt=attempt)
                    run["block"]={"category":"integrity","code":"storage_integrity_failure","node_id":item.get("node"),"attempt":attempt,"human_revisable":False,"allowed_revision_targets":[]}
                    self._bump(db,item["run_id"],run)
        # Files committed by rename before a crash but never referenced by SQLite are
        # explicitly marked uncommitted. Presence alone never promotes them.
        referenced=set()
        with self.db() as db:
            referenced|={(self.run_dir(r[0])/r[1]).resolve() for r in db.execute("SELECT run,path FROM artifacts")}
            for row in db.execute("SELECT run_id,document FROM model_calls"):
                doc=json.loads(row[1]); base=self.run_dir(row[0])
                for key in ("request_path","response_path"):
                    if doc.get(key): referenced.add((base/doc[key]).resolve())
            for row in db.execute("SELECT run_id,document FROM repairs"):
                doc=json.loads(row[1]);
                if doc.get("path"): referenced.add((self.run_dir(row[0])/doc["path"]).resolve())
        orphans=[]
        if self.runs_root.exists():
            for folder in self.runs_root.iterdir():
                if not folder.is_dir(): continue
                for sub in (folder/"artifacts",folder/"attempts"):
                    if not sub.exists(): continue
                    for p in sub.rglob("*"):
                        if p.is_file() and ".tmp-" not in p.name and p.resolve() not in referenced and p.name not in {"attempt.json"}:
                            orphans.append(str(p.resolve()))
        marker=self.root/"startup-uncommitted-files.json"
        if orphans: _atomic_replace(marker,json.dumps({"uncommitted":sorted(orphans)},indent=2)+"\n")
        elif marker.exists(): marker.unlink()
        for run in self.runs():
            if self.run_dir(run["id"]).exists(): self.rebuild_derived(run["id"])

    def download_path(self,rid,rel):
        base=self.run_dir(rid).resolve(); path=(base/rel).resolve()
        if not path.is_relative_to(base) or not path.is_file(): raise Fault("file_not_found")
        # authoritative or derived allow-list only
        allowed={"input.json","run-config/resolved.json","manifest.json","output.md","logs/model-usage.json"}
        with self.db() as db:
            allowed|={r[0] for r in db.execute("SELECT path FROM artifacts WHERE run=?",(rid,))}
            allowed|={json.loads(r[0]).get("request_path") for r in db.execute("SELECT document FROM model_calls WHERE run_id=?",(rid,))}
            allowed|={json.loads(r[0]).get("response_path") for r in db.execute("SELECT document FROM model_calls WHERE run_id=?",(rid,))}
            allowed|={json.loads(r[0]).get("path") for r in db.execute("SELECT document FROM repairs WHERE run_id=?",(rid,))}
        allowed.discard(None)
        if str(path.relative_to(base)) not in allowed: raise Fault("file_not_committed")
        return path

    def delete_run(self,rid):
        run=self.run(rid)
        if run.get("status") in {"running","waiting_model"}: raise Fault("run_active")
        with self.db() as db:
            for table,col in [("artifacts","run"),("events","run"),("step_attempts","run_id"),("model_calls","run_id"),("physical_calls","run_id"),("operations","run_id"),("repairs","run_id"),("semantic_revisions","run_id"),("tool_calls","run_id"),("review_events","run_id")]: db.execute(f"DELETE FROM {table} WHERE {col}=?",(rid,))
            db.execute("DELETE FROM active_corpora WHERE scope=?",(rid,)); db.execute("DELETE FROM corpora WHERE scope=?",(rid,)); db.execute("DELETE FROM runs WHERE id=?",(rid,))
        shutil.rmtree(self.run_dir(rid),ignore_errors=True)
