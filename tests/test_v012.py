from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,re,sqlite3,unittest,uuid

from localmedbot.audit import audit_json
from localmedbot.contracts import Fault
from localmedbot.errors import present_error
from localmedbot.relocation import relocate_legacy
from localmedbot.runtime import generated_run_id, resolve_retry_limits
from localmedbot.service import Service
from localmedbot.storage import Store
from localmedbot.usage import aggregate_usage

ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT/'applications'; PROFILES=ROOT/'model_profiles'; GUIDES=ROOT/'guideline_sets'; FIXTURES=ROOT/'tests/fixtures/steps'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES); self.addCleanup(self.s.close)

    def demo(self):
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard')
        return rid,self.s.runner.advance(rid)

    def captured(self,node):
        rid,run=self.demo(); doc=self.s.capture_fixture(rid,node,run['attempts'][node])
        doc.update(id=f'v012.{node}.{uuid.uuid4().hex[:8]}',version=1,data_suitability='synthetic')
        self.s.save_scratch_fixture(doc); return doc

    def tape(self,doc,contents):
        return {'tape_schema_version':1,'id':f"v012.{doc['node_id']}.{uuid.uuid4().hex[:8]}",'version':1,'workflow_id':doc['workflow_id'],'node_id':doc['node_id'],'step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':i+1,'content':c} for i,c in enumerate(contents)]}


class AuditAndRepairTests(Base):
    def test_audit_is_syntax_then_all_schema_then_protocol(self):
        schema={'type':'object','required':['a','b'],'additionalProperties':False,'properties':{'a':{'type':'integer'},'b':{'type':'string'}}}
        called=[]
        def protocol(value):
            called.append(True)
            return [{'code':'protocol.bad','kind':'protocol','instance_location':'/a','problem':'bad protocol','required_correction':'fix protocol'}]
        value,audit=audit_json('{broken',schema,[protocol])
        self.assertIsNone(value); self.assertEqual(audit['phase'],'syntax'); self.assertEqual(len(audit['findings']),1); self.assertEqual(called,[])
        _,audit=audit_json('{"a":"x","c":1}',schema,[protocol])
        self.assertEqual(audit['phase'],'schema'); self.assertGreaterEqual(len(audit['findings']),3); self.assertEqual(called,[])
        _,audit=audit_json('{"a":1,"b":"ok"}',schema,[protocol])
        self.assertEqual(audit['phase'],'protocol'); self.assertEqual([x['code'] for x in audit['findings']],['protocol.bad']); self.assertEqual(len(called),1)

    def test_shared_repair_facility_repairs_generator_and_checker_in_same_attempt(self):
        for node in ('draft','facts_check'):
            with self.subTest(node=node):
                doc=self.captured(node)
                if node=='draft':
                    valid={'title':'Fixture letter','claims':[{'id':f['id'],'text':f['text'],'evidence_refs':f['evidence_refs']} for f in doc['resolved_inputs']['source']['facts']]}
                else:
                    valid={'status':'pass','findings':[]}
                tape=self.tape(doc,['{broken',json.dumps(valid)])
                rid=self.s.run_step('clinical_letter',node,doc,'clinical_letter.recorded.default',tape=tape,developer=True,retry_overrides={'output_repair_retries':1})
                done=self.s.runner.advance(rid); self.assertEqual(done['status'],'completed'); self.assertEqual(done['attempts'][node],1)
                calls=self.s.store.model_calls(rid); repairs=self.s.store.repairs(rid); ops=self.s.store.operations(rid)
                self.assertEqual([c['purpose'] for c in calls],['initial','output_repair']); self.assertEqual(len(ops),1); self.assertEqual(len(repairs),1); self.assertEqual(repairs[0]['outcome'],'corrected')
                self.assertIn(repairs[0]['rendered_feedback'],calls[1]['messages'][-1]['content'])
                self.assertEqual(calls[0]['raw_response'],'{broken')
                if node=='facts_check': self.assertEqual(self.s.store.semantic_revisions(rid),[])

    def test_final_allowed_output_repair_succeeds_and_identical_failures_exhaust_exactly(self):
        doc=self.captured('draft')
        valid={'title':'Fixture letter','claims':[{'id':f['id'],'text':f['text'],'evidence_refs':f['evidence_refs']} for f in doc['resolved_inputs']['source']['facts']]}
        tape=self.tape(doc,['{}','{}','{}',json.dumps(valid)])
        rid=self.s.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=tape,developer=True,retry_overrides={'output_repair_retries':3})
        done=self.s.runner.advance(rid); self.assertEqual(done['status'],'completed'); self.assertEqual(done['attempts']['draft'],1)
        self.assertEqual(len([r for r in self.s.store.repairs(rid) if r.get('next_request_id')]),3)
        self.assertEqual(len(self.s.store.model_calls(rid)),4)
        tape=self.tape(doc,['{}','{}','{}','{}'])
        rid=self.s.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=tape,developer=True,retry_overrides={'output_repair_retries':3})
        done=self.s.runner.advance(rid); self.assertEqual(done['status'],'blocked'); self.assertEqual(done['error']['code'],'schema_invalid'); self.assertEqual(done['attempts']['draft'],1)
        repairs=self.s.store.repairs(rid); self.assertEqual(len([r for r in repairs if r.get('next_request_id')]),3); self.assertEqual(repairs[-1]['outcome'],'exhausted')

    def test_semantic_revision_creates_new_target_attempt_and_links_feedback(self):
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard',developer=True,retry_overrides={'semantic_revision_retries':1})
        run=self.s.store.run(rid); run['snapshot']['recording']['draft_check']=[{'status':'fail','findings':[{'code':'coverage','severity':'error','detail':'include F1','ids':['F1']}]},{'status':'pass','findings':[]}]; self.s.store.put_run(run)
        done=self.s.runner.advance(rid); self.assertEqual(done['status'],'waiting_review'); self.assertEqual(done['attempts']['draft'],2); self.assertEqual(done['attempts']['draft_check'],2)
        links=self.s.store.semantic_revisions(rid); self.assertEqual(len(links),1); link=links[0]; self.assertEqual(link['outcome'],'corrected'); self.assertTrue(link['next_operation_id'])
        second=self.s.store.step_attempt(rid,'draft',2); self.assertEqual(second['resolved_inputs']['revision_feedback'],link['feedback_envelope'])

    def test_retry_precedence_and_freezing(self):
        snap=self.s.load('clinical_letter'); facts=next(x for x in snap['workflow']['nodes'] if x['id']=='facts'); check=next(x for x in snap['workflow']['nodes'] if x['id']=='facts_check')
        facts['repairs']=1; check['review']['max_revisions']=1; snap['policy']['output_repair_retries']=2; snap['policy']['semantic_revision_retries']=2
        limits=resolve_retry_limits(snap,{'output_repair_retries':0,'semantic_revision_retries':4})
        self.assertEqual(limits['output_repair_by_node']['facts'],0); self.assertEqual(limits['semantic_revision_by_check']['facts_check'],4)
        limits=resolve_retry_limits(snap,{})
        self.assertEqual(limits['output_repair_by_node']['facts'],1); self.assertEqual(limits['semantic_revision_by_check']['facts_check'],1)
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True,retry_overrides={'output_repair_retries':0})
        frozen=deepcopy(self.s.store.run(rid)['retry_limits']); self.s.execution_defaults['output_repair_retries']=99
        self.assertEqual(self.s.store.run(rid)['retry_limits'],frozen)


class AccountingAndStorageTests(Base):
    def test_usage_distinguishes_replay_self_provider_and_partial_reporting(self):
        rid,run=self.demo(); usage=self.s.inspect(rid)['usage']; self.assertEqual(usage['physical_calls'],0); self.assertGreater(usage['replay_calls'],0); self.assertIsNone(usage['usage']['totals']['total_tokens'])
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True); self.s.runner.advance(rid)
        first=self.s.inspect(rid)['usage']; second=self.s.inspect(rid)['usage']; self.assertEqual(first['self_handoffs'],1); self.assertEqual(first,second)
        synthetic=aggregate_usage(
            [{'operation_id':'o','request_id':'r1','node_id':'n','purpose':'initial'},{'operation_id':'o','request_id':'r2','node_id':'n','purpose':'output_repair'}],
            [{'call_id':'c1','request_id':'r1','executor':'openrouter','dispatched':True,'response_metadata':{'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5,'cost':0.01,'currency':'USD'},'duration':0.2}},{'call_id':'c2','request_id':'r2','executor':'openrouter','dispatched':True,'retry_kind':'transport','response_metadata':None}],[])
        self.assertEqual(synthetic['physical_calls'],2); self.assertTrue(synthetic['usage']['partial']); self.assertEqual(synthetic['usage']['totals']['total_tokens'],5); self.assertEqual(synthetic['transport_retries'],1)

    def test_run_identity_and_authoritative_files(self):
        rid=generated_run_id('clinical_letter'); self.assertRegex(rid,r'^\d{8}T\d{12}Z_clinical_letter_[0-9a-f]{8}$')
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True)
        folder=self.s.store.run_dir(rid); self.assertTrue((folder/'input.json').is_file()); self.assertTrue((folder/'run-config/resolved.json').is_file()); self.assertEqual(self.s.store.run(rid)['run_folder'],str(folder))
        before=self.s.inspect(rid)['inspection_revision']; self.s.store.event(rid,'synthetic_visible_event',{}); after=self.s.inspect(rid)['inspection_revision']; self.assertGreater(after,before)

    def test_crash_windows_mark_orphans_and_rebuild_derived_views(self):
        import localmedbot.storage as storage
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES); rid=s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True); run=s.store.run(rid)
            original=storage._immutable_write
            def after_rename(path,text): original(path,text); raise OSError('fault after rename')
            with patch('localmedbot.storage._immutable_write',after_rename), self.assertRaises(OSError): s.store.commit(run,'synthetic',{'x':1},{})
            self.assertEqual([a for a in s.store.inspection_snapshot(rid)['artifact_history'] if a['id']=='synthetic'],[]); s.close()
            st=Store(td,writer=True,runs_root=Path(td)/'runs'); self.assertTrue((Path(td)/'startup-uncommitted-files.json').is_file()); st.close()
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES); rid=s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True); run=s.store.run(rid)
            real=s.store.rebuild_derived
            with patch.object(s.store,'rebuild_derived',side_effect=OSError('after db commit')), self.assertRaises(OSError): s.store.commit(run,'synthetic',{'x':1},{})
            s.close(); st=Store(td,writer=True,runs_root=Path(td)/'runs'); self.assertEqual(st.artifact(rid,'synthetic')['payload'],{'x':1}); self.assertTrue((st.run_dir(rid)/'manifest.json').is_file()); st.close()

    def test_startup_blocks_missing_committed_model_payload(self):
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES)
            rid=s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True); s.runner.advance(rid)
            call=s.store.model_calls(rid)[0]; path=s.store.run_dir(rid)/call['request_path']; self.assertTrue(path.is_file()); path.unlink(); s.close()
            st=Store(td,writer=True,runs_root=Path(td)/'runs'); run=st.run(rid); self.assertEqual(run['status'],'blocked'); self.assertEqual(run['error']['code'],'storage_integrity_failure'); self.assertIn('explanation',run['error']); st.close()

    def test_pending_self_repair_survives_restart_without_recharging(self):
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES); rid=s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True,retry_overrides={'output_repair_retries':3}); s.runner.advance(rid)
            first=s.self_handoff(rid); s.self_submit(rid,{'contract_version':first['contract_version'],'request_id':first['request_id'],'content':'{broken'})
            repair=s.self_handoff(rid); before=(repair['request_id'],repair['repair_ordinal'],len(s.store.repairs(rid)),len(s.store.model_calls(rid))); s.close()
            s2=Service(APPS,td,PROFILES,GUIDES,FIXTURES); self.addCleanup(s2.close); resumed=s2.self_handoff(rid); after=(resumed['request_id'],resumed['repair_ordinal'],len(s2.store.repairs(rid)),len(s2.store.model_calls(rid)))
            self.assertEqual(before,after); self.assertEqual(resumed['purpose'],'output_repair'); self.assertEqual(resumed['repair_feedback'],repair['repair_feedback'])

    def test_persisted_error_presentation_is_historical_and_legacy_fallback_is_labelled(self):
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True)
        run=self.s.store.run(rid); stored=present_error(Fault('invalid_input'),stage='facts',attempt=1); run['error']=deepcopy(stored); self.s.store.put_run(run)
        with patch.dict('localmedbot.errors._PRESENTATIONS',{'invalid_input':('Changed later','Changed remedy')},clear=False):
            self.assertEqual(self.s.inspect(rid)['current_error']['explanation'],stored['explanation'])
        run=self.s.store.run(rid); run['error']={'code':'invalid_input','detail':'legacy row'}; self.s.store.put_run(run); fallback=self.s.inspect(rid)['current_error']
        self.assertEqual(fallback['presentation_source'],'current_fallback_for_legacy_error'); self.assertTrue(fallback['fallback'])


class RelocationTests(unittest.TestCase):
    def _legacy_db(self,source):
        source.mkdir(parents=True); db=sqlite3.connect(source/'state.sqlite')
        db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE runs(id TEXT PRIMARY KEY,document TEXT NOT NULL); CREATE TABLE artifacts(run TEXT,node TEXT,revision INTEGER,path TEXT,metadata TEXT,PRIMARY KEY(run,node,revision)); CREATE TABLE step_attempts(run_id TEXT,node_id TEXT,attempt INTEGER,document TEXT NOT NULL,PRIMARY KEY(run_id,node_id,attempt)); CREATE TABLE model_calls(request_id TEXT PRIMARY KEY,run_id TEXT,node_id TEXT,attempt INTEGER,call_index INTEGER,document TEXT NOT NULL);')
        db.execute("INSERT INTO meta VALUES('storage_schema_version','1')"); db.commit(); db.close()

    def test_relocation_is_journalled_restartable_and_does_not_overwrite_conflicts(self):
        with TemporaryDirectory() as td:
            root=Path(td); source=root/'.localmedbot'; state=root/'state'; runs=root/'runs'; scratch=root/'tests/fixtures/scratch'; self._legacy_db(source)
            doc=relocate_legacy(source,state,runs,scratch); self.assertTrue(doc['completed']); self.assertTrue((state/'relocation-journal.json').is_file()); self.assertFalse(source.exists())
            again=relocate_legacy(source,state,runs,scratch); self.assertEqual(again['migration_id'],doc['migration_id'])
        with TemporaryDirectory() as td:
            root=Path(td); source=root/'.localmedbot'; state=root/'state'; runs=root/'runs'; scratch=root/'tests/fixtures/scratch'; self._legacy_db(source); state.mkdir(); (state/'unrelated.txt').write_text('keep')
            with self.assertRaises(Fault) as exc: relocate_legacy(source,state,runs,scratch)
            self.assertEqual(exc.exception.code,'relocation_destination_not_empty'); self.assertEqual((state/'unrelated.txt').read_text(),'keep')


if __name__=='__main__': unittest.main()
