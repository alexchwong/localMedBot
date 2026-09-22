"""Offline, journalled relocation from the legacy single-root .localmedbot layout."""
from __future__ import annotations
from contextlib import ExitStack
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,time,uuid
from .contracts import Fault
from .storage import WriterGuard,_atomic_replace,_immutable_write

JOURNAL_VERSION=1


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def _journal(path,doc): _atomic_replace(path,json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True)+'\n')


def _copy_verified(src,dst,expected=None):
    src,dst=Path(src),Path(dst); dst.parent.mkdir(parents=True,exist_ok=True); digest=expected or _sha(src)
    if dst.exists():
        if _sha(dst)!=digest: raise Fault('relocation_conflict',str(dst))
        return digest
    tmp=dst.with_name(dst.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with src.open('rb') as r,tmp.open('xb') as w:
            shutil.copyfileobj(r,w); w.flush(); os.fsync(w.fileno())
        if _sha(tmp)!=digest: raise Fault('relocation_validation_failed',str(src))
        os.rename(tmp,dst)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass
    return digest


def _db_rows(db,sql):
    db.row_factory=sqlite3.Row
    return [dict(r) for r in db.execute(sql).fetchall()]


def relocate_legacy(source,state_root,runs_root,scratch_root):
    source=Path(source).resolve(); state=Path(state_root).resolve(); runs=Path(runs_root).resolve(); scratch=Path(scratch_root).resolve()
    if source in {state,runs,scratch} or state==runs: raise Fault('relocation_root_invalid')
    state.mkdir(parents=True,exist_ok=True); runs.mkdir(parents=True,exist_ok=True); scratch.mkdir(parents=True,exist_ok=True)
    journal_path=state/'relocation-journal.json'
    existing=None
    if journal_path.exists():
        try: existing=json.loads(journal_path.read_text(encoding='utf-8'))
        except Exception: raise Fault('relocation_journal_invalid',str(journal_path)) from None
        if existing.get('version')!=JOURNAL_VERSION or existing.get('source')!=str(source) or existing.get('state_root')!=str(state) or existing.get('runs_root')!=str(runs): raise Fault('relocation_journal_conflict')
        if existing.get('completed'): return existing
    if not source.is_dir() or not (source/'state.sqlite').is_file(): raise Fault('legacy_data_not_found',str(source))
    if existing is None:
        unrelated=[p for p in state.iterdir() if p.name not in {'writer.lock'}]
        if unrelated: raise Fault('relocation_destination_not_empty',str(state))
        if any(runs.iterdir()): raise Fault('relocation_destination_not_empty',str(runs))
    # Lock both state directories in stable lexical order. The source runtime lock is authoritative.
    guards=[]
    try:
        for root in sorted({source,state},key=lambda p:str(p)):
            guards.append(WriterGuard(root,True))
        doc=existing or {'version':JOURNAL_VERSION,'migration_id':uuid.uuid4().hex,'source':str(source),'state_root':str(state),'runs_root':str(runs),'scratch_root':str(scratch),'created':time.time(),'phase':'inventory','items':[], 'unknown':[], 'database_activation':'pending','completed':False}
        if not existing:
            srcdb=sqlite3.connect(source/'state.sqlite'); srcdb.row_factory=sqlite3.Row
            try:
                artifacts=_db_rows(srcdb,'SELECT run,node,revision,path,metadata FROM artifacts ORDER BY run,node,revision') if srcdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone() else []
                runs_rows=_db_rows(srcdb,'SELECT id,document FROM runs ORDER BY rowid') if srcdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() else []
                attempts=_db_rows(srcdb,'SELECT run_id,node_id,attempt,document FROM step_attempts ORDER BY run_id,node_id,attempt') if srcdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='step_attempts'").fetchone() else []
                calls=_db_rows(srcdb,'SELECT request_id,run_id,node_id,attempt,call_index,document FROM model_calls ORDER BY rowid') if srcdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone() else []
            finally: srcdb.close()
            doc['runs']=[r['id'] for r in runs_rows]; doc['artifact_mappings']=[]
            known={str((source/'state.sqlite').resolve()),str((source/'writer.lock').resolve())}
            for a in artifacts:
                sp=(source/a['path']).resolve()
                if not sp.is_relative_to(source) or not sp.is_file(): raise Fault('relocation_source_missing',str(sp))
                suffix=sp.suffix or '.json'; rel=Path('artifacts')/a['node']/f"{int(a['revision']):04d}_{sp.stem}{suffix}"
                dest=runs/a['run']/rel; digest=_sha(sp)
                doc['artifact_mappings'].append({'run_id':a['run'],'node':a['node'],'revision':a['revision'],'source':str(sp),'destination':str(dest),'relative':str(rel),'sha256':digest,'status':'pending'}); known.add(str(sp))
            # Materialise the old DB-held run inputs/configuration/attempt request documents into the new per-run folders.
            doc['derived_files']=[]
            for r in runs_rows:
                run=json.loads(r['document']); folder=runs/r['id']
                payloads={'input.json':run.get('input',{}),'run-config/resolved.json':{'snapshot':run.get('snapshot'), 'profile':run.get('profile'), 'runtime_version':run.get('runtime_version'), 'run_contract_version':run.get('run_contract_version')}}
                for rel,value in payloads.items(): doc['derived_files'].append({'run_id':r['id'],'destination':str(folder/rel),'content':json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n','status':'pending'})
            for a in attempts:
                rel=Path('attempts')/a['node_id']/f"{int(a['attempt']):04d}"/'attempt.json'; doc['derived_files'].append({'run_id':a['run_id'],'destination':str(runs/a['run_id']/rel),'content':json.dumps(json.loads(a['document']),ensure_ascii=False,indent=2,sort_keys=True)+'\n','status':'pending'})
            for c in calls:
                rel=Path('attempts')/c['node_id']/f"{int(c['attempt']):04d}"/'legacy-calls'/f"{c['request_id']}.json"; doc['derived_files'].append({'run_id':c['run_id'],'destination':str(runs/c['run_id']/rel),'content':json.dumps(json.loads(c['document']),ensure_ascii=False,indent=2,sort_keys=True)+'\n','status':'pending'})
            doc['scratch_mappings']=[]
            legacy_scratch=source/'fixtures'/'scratch'
            if legacy_scratch.exists():
                for sp in legacy_scratch.rglob('*'):
                    if sp.is_file():
                        dp=scratch/sp.relative_to(legacy_scratch); doc['scratch_mappings'].append({'source':str(sp.resolve()),'destination':str(dp.resolve()),'sha256':_sha(sp),'status':'pending'}); known.add(str(sp.resolve()))
            # WAL/SHM are never copied blindly; a backup below produces a closed consistent DB.
            for extra in (source/'state.sqlite-wal',source/'state.sqlite-shm'):
                if extra.exists(): known.add(str(extra.resolve()))
            doc['unknown']=[str(p.resolve()) for p in source.rglob('*') if p.is_file() and str(p.resolve()) not in known]
            doc['phase']='copy'; _journal(journal_path,doc)
        # Copy all immutable artifacts and scratch files, reconciling bytes if a prior copy completed before journalling.
        for group in ('artifact_mappings','scratch_mappings'):
            for item in doc.get(group,[]):
                _copy_verified(item['source'],item['destination'],item['sha256']); item['status']='validated'; _journal(journal_path,doc)
        for item in doc.get('derived_files',[]):
            dst=Path(item['destination']); text=item['content']; digest=hashlib.sha256(text.encode()).hexdigest()
            if dst.exists():
                if _sha(dst)!=digest: raise Fault('relocation_conflict',str(dst))
            else: _immutable_write(dst,text)
            item['sha256']=digest; item['status']='validated'; _journal(journal_path,doc)
        doc['phase']='database_prepare'; _journal(journal_path,doc)
        prepared=state/'state.sqlite.prepared'
        if not prepared.exists():
            src=sqlite3.connect(source/'state.sqlite'); dst=sqlite3.connect(prepared)
            try: src.backup(dst); dst.commit()
            finally: src.close(); dst.close()
        # Reconcile the prepared DB idempotently.
        db=sqlite3.connect(prepared)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('relocation_migration_id',?)",(doc['migration_id'],))
            for item in doc.get('artifact_mappings',[]): db.execute('UPDATE artifacts SET path=? WHERE run=? AND node=? AND revision=?',(item['relative'],item['run_id'],item['node'],item['revision']))
            # Scratch fixture record paths are mutable registry metadata and must follow moved files.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_records'").fetchone():
                rows=db.execute('SELECT fixture_id,version,document FROM fixture_records').fetchall()
                mapping={x['source']:x['destination'] for x in doc.get('scratch_mappings',[])}
                for fid,ver,raw in rows:
                    try: rec=json.loads(raw)
                    except Exception: continue
                    old=str(Path(rec.get('path','')).resolve()) if rec.get('path') else ''
                    if old in mapping: rec['path']=mapping[old]; db.execute('UPDATE fixture_records SET document=? WHERE fixture_id=? AND version=?',(json.dumps(rec,ensure_ascii=False,sort_keys=True,separators=(',',':')),fid,ver))
            db.commit()
        finally: db.close()
        doc['phase']='activation'; _journal(journal_path,doc)
        active=state/'state.sqlite'
        if active.exists():
            adb=sqlite3.connect(active)
            try:
                row=adb.execute("SELECT value FROM meta WHERE key='relocation_migration_id'").fetchone() if adb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() else None
            finally: adb.close()
            if not row or row[0]!=doc['migration_id']: raise Fault('relocation_conflict',str(active))
        else: os.replace(prepared,active)
        doc['database_activation']='active'; doc['phase']='validate'; _journal(journal_path,doc)
        adb=sqlite3.connect(active)
        try:
            row=adb.execute("SELECT value FROM meta WHERE key='relocation_migration_id'").fetchone()
            if not row or row[0]!=doc['migration_id']: raise Fault('relocation_validation_failed','database identity')
            for item in doc.get('artifact_mappings',[]):
                if not Path(item['destination']).is_file() or _sha(item['destination'])!=item['sha256']: raise Fault('relocation_validation_failed',item['destination'])
        finally: adb.close()
        doc['phase']='cleanup'; _journal(journal_path,doc)
        # Remove only journal-owned source copies after successful activation and validation.
        for group in ('artifact_mappings','scratch_mappings'):
            for item in doc.get(group,[]):
                sp=Path(item['source'])
                if sp.exists() and _sha(sp)==item['sha256']: sp.unlink(); item['cleanup']='removed'
                _journal(journal_path,doc)
        # The source DB remains until the very end, preserving recovery material through all earlier phases.
        for sp in (source/'state.sqlite-wal',source/'state.sqlite-shm',source/'state.sqlite'):
            if sp.exists(): sp.unlink()
        for d in sorted([p for p in source.rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
            try: d.rmdir()
            except OSError: pass
        doc['phase']='complete'; doc['completed']=True; doc['completed_at']=time.time(); doc['source_directory_removed']=False
        try: source.rmdir(); doc['source_directory_removed']=True
        except OSError: pass
        _journal(journal_path,doc); return doc
    finally:
        for g in reversed(guards): g.close()
        # The lock file is a coordination artifact, not migrated user data. Once both
        # runtime locks are released, remove it and the legacy root only if nothing
        # else remains. Unknown files are deliberately left in place.
        try:
            if "doc" in locals() and doc.get("completed"):
                lock=source/"writer.lock"
                if lock.exists(): lock.unlink()
                try:
                    source.rmdir(); doc["source_directory_removed"]=True
                except OSError:
                    doc["source_directory_removed"]=False
                _journal(journal_path,doc)
        except OSError:
            pass
