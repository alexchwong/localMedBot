"""SQLite state and immutable JSON artifacts; no application semantics."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import time
import uuid
from .contracts import Fault


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.dbpath = self.root / "state.sqlite"
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(run TEXT, node TEXT, revision INTEGER, path TEXT, metadata TEXT,
                PRIMARY KEY(run,node,revision));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, time REAL, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS corpora(id TEXT PRIMARY KEY, scope TEXT, name TEXT, profile TEXT, sources TEXT, items TEXT);
            CREATE TABLE IF NOT EXISTS active_corpora(scope TEXT, name TEXT, id TEXT, PRIMARY KEY(scope,name));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.dbpath, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_run(self, run):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (run["id"], encode(run)))

    def run(self, rid):
        with self.db() as db:
            row = db.execute("SELECT document FROM runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise Fault("run_not_found")
        return json.loads(row[0])

    def runs(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT document FROM runs ORDER BY rowid DESC")]

    def event(self, rid, kind, data):
        with self.db() as db:
            db.execute("INSERT INTO events(run,time,kind,data) VALUES (?,?,?,?)", (rid, time.time(), kind, encode(data)))

    def events(self, rid):
        with self.db() as db:
            return [dict(id=r[0], time=r[1], kind=r[2], data=json.loads(r[3])) for r in
                    db.execute("SELECT id,time,kind,data FROM events WHERE run=? ORDER BY id", (rid,))]

    def next_revision(self, rid, node):
        with self.db() as db:
            return db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM artifacts WHERE run=? AND node=?", (rid,node)).fetchone()[0]

    def commit(self, run, node, payload, metadata):
        metadata = {**metadata, "run_id": run["id"], "runtime_version": run["runtime_version"], "application_version": run["snapshot"]["manifest"].get("version")}
        rev = self.next_revision(run["id"], node)
        # Files use random names; no user-supplied path components.
        folder = self.root / "artifacts"
        folder.mkdir(exist_ok=True)
        path = folder / (uid() + ".json")
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            f.write(encode(payload)); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.before_commit()  # injectable crash boundary; orphan file is harmless
        run["active"][node] = rev
        run["nodes"][node] = "complete"
        with self.db() as db:
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?)", (run["id"],node,rev,str(path.relative_to(self.root)),encode(metadata)))
            db.execute("UPDATE runs SET document=? WHERE id=?", (encode(run),run["id"]))
        return rev

    def before_commit(self):
        pass

    def artifact(self, rid, node, revision=None):
        if revision is None:
            revision = self.run(rid)["active"].get(node)
        with self.db() as db:
            row = db.execute("SELECT path,metadata FROM artifacts WHERE run=? AND node=? AND revision=?", (rid,node,revision)).fetchone()
        if not row:
            raise Fault("artifact_not_found", node)
        return {"id": node, "revision": revision, "payload": json.loads((self.root / row[0]).read_text()), "metadata": json.loads(row[1])}

    def corpus(self, cid):
        with self.db() as db:
            row = db.execute("SELECT * FROM corpora WHERE id=?", (cid,)).fetchone()
        if not row:
            raise Fault("corpus_not_found")
        return {k: json.loads(row[k]) if k in ["profile","sources","items"] else row[k] for k in row.keys()}

    def active_corpus(self, scope, name):
        with self.db() as db:
            row = db.execute("SELECT id FROM active_corpora WHERE scope=? AND name=?", (scope,name)).fetchone()
        return row[0] if row else None

    def publish(self, scope, name, profile, sources, items):
        # Equality to existing immutable inputs gives idempotency without output snapshots.
        with self.db() as db:
            row = db.execute("SELECT id FROM corpora WHERE scope=? AND name=? AND profile=? AND sources=? AND items=?",
                             (scope,name,encode(profile),encode(sources),encode(items))).fetchone()
            cid = row[0] if row else uid()
            if not row:
                db.execute("INSERT INTO corpora VALUES (?,?,?,?,?,?)", (cid,scope,name,encode(profile),encode(sources),encode(items)))
            db.execute("INSERT OR REPLACE INTO active_corpora VALUES (?,?,?)", (scope,name,cid))
        return cid
