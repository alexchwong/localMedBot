"""Versioned SQLite state and immutable JSON artifacts."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json, os, sqlite3, time, uuid, shutil
from .contracts import Fault
from . import STORAGE_SCHEMA_VERSION


def uid(): return uuid.uuid4().hex

def encode(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


class WriterGuard:
    def __init__(self, root: Path, enabled: bool):
        self.enabled=enabled; self.handle=None
        if not enabled: return
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
    def __init__(self, root, writer=True):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.guard=WriterGuard(self.root,writer)
        self.dbpath=self.root/"state.sqlite"
        existed=self.dbpath.exists()
        self.writer=writer
        version=None
        legacy_existing=False
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
        if writer or not existed:
            self._migrate(version)

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
            CREATE TABLE IF NOT EXISTS model_calls(request_id TEXT PRIMARY KEY,run_id TEXT,node_id TEXT,attempt INTEGER,call_index INTEGER,document TEXT NOT NULL,
                UNIQUE(run_id,node_id,attempt,call_index));
            CREATE TABLE IF NOT EXISTS tool_calls(run_id TEXT,node_id TEXT,attempt INTEGER,tool_index INTEGER,document TEXT NOT NULL,
                PRIMARY KEY(run_id,node_id,attempt,tool_index));
            CREATE TABLE IF NOT EXISTS review_events(run_id TEXT,request_id TEXT,document TEXT NOT NULL,PRIMARY KEY(run_id,request_id));
            CREATE TABLE IF NOT EXISTS guideline_sets(set_id TEXT PRIMARY KEY,document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guideline_releases(set_id TEXT,release_id TEXT,document TEXT NOT NULL,PRIMARY KEY(set_id,release_id));
            CREATE TABLE IF NOT EXISTS fixture_records(fixture_id TEXT,version INTEGER,document TEXT NOT NULL,PRIMARY KEY(fixture_id,version));
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('storage_schema_version',?)",(str(STORAGE_SCHEMA_VERSION),))

    def put_run(self,run):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)",(run["id"],encode(run)))
    def run(self,rid):
        with self.db() as db: row=db.execute("SELECT document FROM runs WHERE id=?",(rid,)).fetchone()
        if not row: raise Fault("run_not_found")
        value=json.loads(row[0]); value.setdefault("legacy", "run_contract_version" not in value)
        return value
    def runs(self):
        with self.db() as db: return [json.loads(r[0]) for r in db.execute("SELECT document FROM runs ORDER BY rowid DESC")]
    def event(self,rid,kind,data):
        with self.db() as db: db.execute("INSERT INTO events(run,time,kind,data) VALUES(?,?,?,?)",(rid,time.time(),kind,encode(data)))
    def events(self,rid):
        with self.db() as db: rows=db.execute("SELECT id,time,kind,data FROM events WHERE run=? ORDER BY id",(rid,)).fetchall()
        return [{"id":r[0],"time":r[1],"kind":r[2],"data":json.loads(r[3])} for r in rows]

    def next_revision(self,rid,node):
        with self.db() as db: return db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM artifacts WHERE run=? AND node=?",(rid,node)).fetchone()[0]
    def commit(self,run,node,payload,metadata):
        rev=self.next_revision(run["id"],node)
        folder=self.root/"artifacts"; folder.mkdir(exist_ok=True)
        path=folder/(uid()+".json"); tmp=path.with_suffix(".tmp")
        text=encode(payload)
        with tmp.open("w",encoding="utf-8") as f: f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
        run["active"][node]=rev; run["nodes"][node]="complete"
        md={**metadata,"run_id":run["id"],"runtime_version":run.get("runtime_version"),"application_version":run["snapshot"]["manifest"].get("version")}
        with self.db() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",(run["id"],node,rev,str(path.relative_to(self.root)),encode(md)))
            db.execute("UPDATE runs SET document=? WHERE id=?",(encode(run),run["id"]))
        return rev
    def artifact(self,rid,node,revision=None):
        if revision is None: revision=self.run(rid)["active"].get(node)
        with self.db() as db: row=db.execute("SELECT path,metadata FROM artifacts WHERE run=? AND node=? AND revision=?",(rid,node,revision)).fetchone()
        if not row: raise Fault("artifact_not_found",node)
        return {"id":node,"revision":revision,"payload":json.loads((self.root/row[0]).read_text(encoding="utf-8")),"metadata":json.loads(row[1])}

    def corpus(self,cid):
        with self.db() as db: row=db.execute("SELECT * FROM corpora WHERE id=?",(cid,)).fetchone()
        if not row: raise Fault("corpus_not_found")
        return {k:json.loads(row[k]) if k in {"profile","sources","items"} else row[k] for k in row.keys()}
    def publish(self,scope,name,profile,sources,items,activate=True,cid=None):
        ep,es,ei=encode(profile),encode(sources),encode(items)
        with self.db() as db:
            row=db.execute("SELECT id FROM corpora WHERE scope=? AND name=? AND profile=? AND sources=? AND items=?",(scope,name,ep,es,ei)).fetchone()
            actual=row[0] if row else (cid or uid())
            if row is None:
                existing=db.execute("SELECT profile,sources,items FROM corpora WHERE id=?",(actual,)).fetchone()
                if existing and tuple(existing)!=(ep,es,ei): raise Fault("immutable_release_changed")
                if not existing: db.execute("INSERT INTO corpora VALUES(?,?,?,?,?,?)",(actual,scope,name,ep,es,ei))
            if activate: db.execute("INSERT OR REPLACE INTO active_corpora VALUES(?,?,?)",(scope,name,actual))
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

    def put_step_attempt(self,rid,node,attempt,doc):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO step_attempts VALUES(?,?,?,?)",(rid,node,attempt,encode(doc)))
    def step_attempt(self,rid,node,attempt):
        with self.db() as db: row=db.execute("SELECT document FROM step_attempts WHERE run_id=? AND node_id=? AND attempt=?",(rid,node,attempt)).fetchone()
        return json.loads(row[0]) if row else None

    def put_model_call(self,doc):
        with self.db() as db:
            try: db.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?)",(doc["request_id"],doc["run_id"],doc["node_id"],doc["attempt"],doc["call_index"],encode(doc)))
            except sqlite3.IntegrityError:
                row=db.execute("SELECT document FROM model_calls WHERE run_id=? AND node_id=? AND attempt=? AND call_index=?",(doc["run_id"],doc["node_id"],doc["attempt"],doc["call_index"])).fetchone()
                old=json.loads(row[0])
                if old.get("canonical")!=doc.get("canonical"): raise Fault("resume_contract_mismatch")
                return old
        return doc
    def update_model_call(self,request_id,doc):
        with self.db() as db: db.execute("UPDATE model_calls SET document=? WHERE request_id=?",(encode(doc),request_id))
    def model_call(self,request_id):
        with self.db() as db: row=db.execute("SELECT document FROM model_calls WHERE request_id=?",(request_id,)).fetchone()
        if not row: raise Fault("handoff_not_found")
        return json.loads(row[0])
    def model_call_at(self,rid,node,attempt,index):
        with self.db() as db: row=db.execute("SELECT document FROM model_calls WHERE run_id=? AND node_id=? AND attempt=? AND call_index=?",(rid,node,attempt,index)).fetchone()
        return json.loads(row[0]) if row else None

    def put_tool_call(self,rid,node,attempt,index,doc):
        with self.db() as db:
            row=db.execute("SELECT document FROM tool_calls WHERE run_id=? AND node_id=? AND attempt=? AND tool_index=?",(rid,node,attempt,index)).fetchone()
            if row:
                old=json.loads(row[0])
                if old.get("canonical")!=doc.get("canonical"): raise Fault("resume_contract_mismatch")
                return old
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?)",(rid,node,attempt,index,encode(doc)))
        return doc
    def update_tool_call(self,rid,node,attempt,index,doc):
        with self.db() as db: db.execute("UPDATE tool_calls SET document=? WHERE run_id=? AND node_id=? AND attempt=? AND tool_index=?",(encode(doc),rid,node,attempt,index))


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
        with self.db() as db: db.execute("INSERT INTO review_events VALUES(?,?,?)",(rid,request_id,encode(doc)))

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

    def delete_run(self,rid):
        run=self.run(rid)
        if run.get("status") in {"running","waiting_model"}: raise Fault("run_active")
        with self.db() as db:
            files=[r[0] for r in db.execute("SELECT path FROM artifacts WHERE run=?",(rid,))]
            for table,col in [("artifacts","run"),("events","run"),("step_attempts","run_id"),("model_calls","run_id"),("tool_calls","run_id"),("review_events","run_id")]:
                db.execute(f"DELETE FROM {table} WHERE {col}=?",(rid,))
            db.execute("DELETE FROM active_corpora WHERE scope=?",(rid,))
            db.execute("DELETE FROM corpora WHERE scope=?",(rid,))
            db.execute("DELETE FROM runs WHERE id=?",(rid,))
        for f in files:
            try:(self.root/f).unlink()
            except FileNotFoundError: pass
