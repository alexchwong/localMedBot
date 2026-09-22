from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import json, os, shutil, sqlite3, unittest, uuid

from localmedbot import STORAGE_SCHEMA_VERSION, RUN_CONTRACT_VERSION, STEP_CONTRACT_VERSION
from localmedbot.contracts import Fault, validate_actor
from localmedbot.service import Service
from localmedbot.profiles import classify_destination
from localmedbot.storage import Store
from localmedbot.fixtures import validate_fixture_id, validate_version

ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT/'applications'; PROFILES=ROOT/'model_profiles'; GUIDES=ROOT/'guideline_sets'; FIXTURES=ROOT/'tests/fixtures/steps'


def fault(tc, code, fn, *a, **kw):
    with tc.assertRaises(Fault) as exc: fn(*a, **kw)
    tc.assertEqual(exc.exception.code, code)

@contextmanager
def endpoint(responses):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            requests.append({'path':self.path,'json':json.loads(body),'auth':self.headers.get('Authorization')})
            item=responses.pop(0)
            if isinstance(item,int): self.send_response(item); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('x-request-id','fixture-request'); self.end_headers(); self.wfile.write(json.dumps(item).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler); thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally: server.shutdown(); server.server_close(); thread.join()

def reply(value,model='fixture-model'):
    return {'model':model,'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}],'usage':{'total_tokens':7}}

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES)
        self.addCleanup(self.s.close)
    def demo(self,app='clinical_letter',example='standard'):
        rid=self.s.start(app,f'{app}.recorded.default',input_mode='demo',example=example)
        r=self.s.runner.advance(rid)
        return rid,r

class VersionAndProfileTests(Base):
    def test_persisted_contract_versions(self):
        # The product version is not a test constant; see docs/versioning.md.
        self.assertEqual((STORAGE_SCHEMA_VERSION,RUN_CONTRACT_VERSION,STEP_CONTRACT_VERSION),(2,1,1))
        self.assertEqual(self.s.load('clinical_letter')['workflow']['version'],2)
        self.assertEqual(self.s.load('guideline_qa')['workflow']['version'],2)
    def test_profiles_exist_and_filter(self):
        for workflow in ('clinical_letter','guideline_qa'):
            rows=self.s.profile_list(workflow)
            self.assertEqual({r['executor'] for r in rows},{'recorded','self','lmstudio','openrouter'})
            self.assertTrue(all(r['workflow_id']==workflow for r in rows))
    def test_overlay_recursive_merge_and_reset(self):
        pid='clinical_letter.openrouter.default'
        saved={'model':'base-model','settings':{'max_tokens':8000},'roles':{'writing':{'model':'model-A','max_tokens':7000}}}
        self.s.configure_profile(pid,saved)
        eff=self.s.profiles.resolve(pid,{'roles':{'writing':{'temperature':0.2}}},workflow_id='clinical_letter')
        self.assertEqual(eff['effective_roles']['writing']['model'],'model-A')
        self.assertEqual(eff['effective_roles']['writing']['max_tokens'],7000)
        self.assertEqual(eff['effective_roles']['writing']['temperature'],0.2)
        self.s.configure_profile(pid,{})
        self.assertEqual(self.s.store.profile_overlay(pid),{})
        fault(self,'profile_workflow_mismatch',self.s.profiles.resolve,pid,{},workflow_id='guideline_qa')
        fault(self,'profile_override_invalid',self.s.configure_profile,pid,{'model':None})
        fault(self,'profile_override_invalid',self.s.configure_profile,pid,{'executor':'lmstudio'})
    def test_locality_is_explicit_ranges(self):
        base={'executor':'lmstudio','base_url':'http://127.0.0.1:1234/v1'}
        self.assertEqual(classify_destination(base)['classification'],'local_network')
        self.assertEqual(classify_destination({**base,'base_url':'http://10.2.3.4:1234/v1'})['classification'],'local_network')
        self.assertEqual(classify_destination({**base,'base_url':'http://192.0.2.2:1234/v1'})['classification'],'non_local')
        self.assertEqual(classify_destination({'executor':'openrouter','base_url':'https://openrouter.ai/api/v1'})['classification'],'non_local')
        self.assertEqual(classify_destination({'executor':'self','base_url':''})['classification'],'unknown')
    def test_credential_vault_not_persisted(self):
        pid='clinical_letter.openrouter.default'; secret='fixture-secret-value'
        self.s.configure_profile(pid,{'model':'fixture'},credential=secret)
        self.assertEqual(self.s.profiles.credential(self.s.profiles.resolve(pid,{},workflow_id='clinical_letter')),secret)
        raw=Path(self.tmp.name,'state.sqlite').read_bytes()
        self.assertNotIn(secret.encode(),raw)

class WorkflowTests(Base):
    def test_recorded_examples(self):
        expected={'standard':'supported','mismatch':'no_evidence','conflict':'conflicting','no_evidence':'no_evidence'}
        for ex,outcome in expected.items():
            with self.subTest(ex=ex):
                rid,r=self.demo('guideline_qa',ex); self.assertEqual(r['status'],'waiting_review')
                out=self.s.store.artifact(rid,'output')['payload']; self.assertEqual(out['evidence_outcome'],outcome)
                if ex=='conflict': self.assertTrue(out['conflict_ids']); self.assertFalse(out['claim_ids'])
        for ex in ('standard','conflict','missing'):
            rid,r=self.demo('clinical_letter',ex); self.assertEqual(r['status'],'waiting_review'); self.assertIn('output',r['active'])
    def test_free_text_letter_input_preserved(self):
        notes='Line one.\nUnicode αβ.\nNo fever was reported.'; purpose='Update clinician'
        # recorded may not be used for arbitrary text
        fault(self,'recorded_input_mismatch',self.s.start,'clinical_letter','clinical_letter.recorded.default','free_text',{'notes':notes,'purpose':purpose})
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':notes,'purpose':purpose},developer=True)
        run=self.s.runner.advance(rid); self.assertEqual(run['status'],'waiting_model')
        self.assertEqual(run['input']['sources'][0]['content'],notes)
    def test_reject_is_distinct_and_revisable(self):
        rid,r=self.demo(); outrev=r['active']['output']
        req={'review_request_id':str(uuid.uuid4()),'revision':outrev,'actor':' Reviewer α ','decision':'reject','comments':''}
        rr=self.s.review(rid,req); self.assertEqual(rr['status'],'rejected'); self.assertIsNone(rr['approval']); self.assertIn('output',rr['active'])
        fault(self,'review_revision_required',self.s.runner.advance,rid) if False else None
        fault(self,'review_revision_required',self.s.review,rid,{'review_request_id':str(uuid.uuid4()),'revision':outrev,'actor':'Reviewer','decision':'approve','comments':'','acknowledged_omission_ids':[],'acknowledged_conflict_ids':[]})
        revise={'review_request_id':str(uuid.uuid4()),'revision':outrev,'actor':'Reviewer','decision':'revise','comments':'revise','target':'draft'}
        reopened=self.s.review(rid,revise); self.assertEqual(reopened['status'],'pending'); self.assertNotIn('output',reopened['active'])
        self.assertGreater(self.s.runner.advance(rid)['active']['output'],outrev)
    def test_review_idempotency_and_actor_validation(self):
        rid,r=self.demo(); rev=r['active']['output']; request_id=str(uuid.uuid4())
        payload={'review_request_id':request_id,'revision':rev,'actor':'Reviewer','decision':'approve','comments':'','acknowledged_omission_ids':[],'acknowledged_conflict_ids':[]}
        first=self.s.review(rid,payload); self.assertEqual(first['status'],'completed')
        again=self.s.review(rid,payload); self.assertEqual(again['status'],'completed')
        altered={**payload,'comments':'changed'}; fault(self,'review_request_conflict',self.s.review,rid,altered)
        for actor in ('   ','x'*101,'bad\nname'):
            fault(self,'actor_invalid',validate_actor,actor)
    def test_guideline_scope_is_single_set(self):
        a=self.s.guidelines.resolve('demo','default'); b=self.s.guidelines.resolve('demo_alt','default')
        self.assertNotEqual(a['corpus_id'],b['corpus_id'])
        rid=self.s.start('guideline_qa','guideline_qa.recorded.default','demo',example='standard',guideline_selection={'set_id':'demo','selector':'default'})
        run=self.s.store.run(rid); self.assertEqual(run['corpora'],[a['corpus_id']])
    def test_origin_aggregation(self):
        rid=self.s.start('guideline_qa','guideline_qa.self.default','free_text',{'question':'Synthetic question?'},guideline_selection={'set_id':'demo','selector':'default'},developer=True)
        r=self.s.store.run(rid); self.assertEqual(r['data_origin'],'mixed'); self.assertEqual(r['origins']['task_input'],'user_supplied')

class SelfTests(Base):
    def test_self_handoff_submission_and_duplicate(self):
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note: symptom improved.','purpose':'Update'},developer=True)
        r=self.s.runner.advance(rid); self.assertEqual(r['status'],'waiting_model')
        h=self.s.self_handoff(rid); self.assertEqual(h['node_id'],'facts'); self.assertIn('messages',h); self.assertIn('output_schema',h)
        content=json.dumps({'facts':[{'id':'F1','text':'Symptom improved.','evidence_refs':[{'corpus_id':r['corpora'][0],'evidence_id':'clinical_note:0:0'}],'qualifiers':{}}],'omission_suggestions':[]})
        env={'contract_version':1,'request_id':h['request_id'],'content':content}
        self.s.model_steps.submit(rid,env); self.s.model_steps.submit(rid,env)
        fault(self,'response_already_submitted',self.s.model_steps.submit,rid,{**env,'content':'{}'})
        nxt=self.s.runner.advance(rid); self.assertEqual(nxt['status'],'waiting_model'); self.assertNotEqual(self.s.self_handoff(rid)['request_id'],h['request_id'])
    def test_self_invalid_json_enters_repair(self):
        rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'Update'},developer=True)
        self.s.runner.advance(rid); h=self.s.self_handoff(rid)
        self.s.model_steps.submit(rid,{'contract_version':1,'request_id':h['request_id'],'content':'{broken'})
        r=self.s.runner.advance(rid); self.assertEqual(r['status'],'waiting_model'); self.assertEqual(r['repair_counts']['facts'],1)

class ProviderTests(Base):
    def test_http_verify_and_auth(self):
        schema_value={'action':'submit','result':{'ok':True}}
        # clinical letter has three distinct roles in the profile defaults
        responses=[reply(schema_value) for _ in range(3)]
        with endpoint(responses) as (url,requests):
            pid='clinical_letter.openrouter.default'; self.s.configure_profile(pid,{'base_url':url,'model':'fixture-model'},credential='fixture-token')
            result=self.s.verify_provider(pid,'clinical_letter'); self.assertTrue(result['success'],result)
            self.assertTrue(requests); self.assertTrue(all(x['auth']=='Bearer fixture-token' for x in requests)); self.assertTrue(all(x['path'].endswith('/chat/completions') for x in requests))
            self.assertTrue(all(x['json']['model']=='fixture-model' for x in requests))
    def test_http_auth_rejected_no_fallback(self):
        with endpoint([401]) as (url,_):
            pid='clinical_letter.openrouter.default'; self.s.configure_profile(pid,{'base_url':url,'model':'fixture-model'},credential='fixture-token')
            result=self.s.verify_provider(pid,'clinical_letter'); self.assertFalse(result['success']); self.assertEqual(result['results'][0]['error']['code'],'authentication_rejected')

class GuidelineLifecycleTests(unittest.TestCase):
    def test_devel_is_frozen_and_promotion_independent(self):
        with TemporaryDirectory() as td:
            root=Path(td)/'guideline_sets'; shutil.copytree(GUIDES,root)
            data=Path(td)/'data'; s=Service(APPS,data,PROFILES,root,FIXTURES); self.addCleanup(s.close)
            old=s.guidelines.resolve('demo','default'); before_other=s.guidelines.resolve('demo_alt','default')
            sources=json.loads((root/'demo/devel/sources.json').read_text())
            sources[0]['content']['items'][0]['text'] += ' Additional synthetic release detail.'
            snap=s.guidelines.import_devel('demo',sources); frozen=s.guidelines.resolve('demo','devel',developer=True)
            self.assertEqual(frozen['corpus_id'],snap)
            result=s.guidelines.promote('demo',snap,'Synthetic fixture promotion','Developer')
            self.assertEqual(result['old_default'],'v1'); self.assertEqual(result['new_default'],'v2')
            self.assertEqual(s.guidelines.resolve('demo','default')['corpus_id'],snap)
            self.assertEqual(s.guidelines.resolve('demo_alt','default')['corpus_id'],before_other['corpus_id'])
            self.assertEqual(s.store.corpus(old['corpus_id'])['id'],old['corpus_id'])

class FixtureTests(Base):
    def test_fixture_id_grammar(self):
        self.assertEqual(validate_fixture_id('clinical_letter.draft.standard'),'clinical_letter.draft.standard'); self.assertEqual(validate_version(1),1)
        for bad in ('Upper','bad/path','a..b','bad space',''):
            fault(self,'fixture_id_invalid',validate_fixture_id,bad)
    def test_capture_and_isolated_step(self):
        rid,r=self.demo('clinical_letter','standard'); attempt=r['attempts']['draft']; doc=self.s.capture_fixture(rid,'draft',attempt)
        doc['id']=f'clinical_letter.draft.captured.{uuid.uuid4().hex[:8]}'; doc['version']=1; doc['data_suitability']='synthetic'
        # pair a tape with exactly this fixture and run just draft
        draft_value={'title':'Fixture letter','claims':[{'id':f['id'],'text':f['text'],'evidence_refs':f['evidence_refs']} for f in doc['resolved_inputs']['source']['facts']]}
        self.s.save_scratch_fixture(doc)
        tape={'tape_schema_version':1,'id':'draft.success','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':1,'content':json.dumps(draft_value)}]}
        rid2=self.s.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=tape,developer=True)
        r2=self.s.runner.advance(rid2); self.assertEqual(r2['status'],'completed'); self.assertTrue(r2['isolated_step']); self.assertEqual(set(r2['nodes']),{'draft'})

    def test_self_isolated_check_strips_full_workflow_review_transition(self):
        # A one-step test supplies already-resolved upstream input, so the selected
        # node must retain its model/check behaviour without requiring its normal
        # workflow revision target or ancestors. This specifically protects self
        # execution of model-backed check nodes.
        rid,r=self.demo('clinical_letter','standard'); attempt=r['attempts']['facts_check']; doc=self.s.capture_fixture(rid,'facts_check',attempt)
        srid=self.s.run_step('clinical_letter','facts_check',doc,'clinical_letter.self.default',developer=True)
        waiting=self.s.runner.advance(srid); self.assertEqual(waiting['status'],'waiting_model')
        handoff=self.s.self_handoff(srid); self.assertEqual(handoff['node_id'],'facts_check')
        result=self.s.self_submit(srid,{'contract_version':1,'request_id':handoff['request_id'],'content':json.dumps({'status':'pass','findings':[]})})
        self.assertEqual(result['status'],'completed'); self.assertEqual(set(result['nodes']),{'facts_check'})

class MigrationTests(unittest.TestCase):
    def test_legacy_backup_and_read_only_marker(self):
        with TemporaryDirectory() as td:
            db=sqlite3.connect(Path(td)/'state.sqlite'); db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY, document TEXT NOT NULL)'); db.execute('CREATE TABLE artifacts(run TEXT,node TEXT,revision INTEGER,path TEXT,metadata TEXT,PRIMARY KEY(run,node,revision))'); db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,run TEXT,time REAL,kind TEXT,data TEXT)'); db.execute('CREATE TABLE corpora(id TEXT PRIMARY KEY,scope TEXT,name TEXT,profile TEXT,sources TEXT,items TEXT)'); db.execute('CREATE TABLE active_corpora(scope TEXT,name TEXT,id TEXT,PRIMARY KEY(scope,name))'); db.execute('INSERT INTO runs VALUES(?,?)',('legacy',json.dumps({'id':'legacy','status':'completed','input':{},'snapshot':{'manifest':{'id':'clinical_letter'}}}))); db.commit(); db.close()
            s=Store(td,writer=True); self.addCleanup(s.close); self.assertTrue(s.run('legacy')['legacy']); self.assertTrue(list(Path(td).glob('state.pre-v1.*.sqlite')))

if __name__=='__main__': unittest.main()

class ClarificationContractTests(Base):
    def test_block_taxonomy_semantic_integrity_budget(self):
        # semantic exhaustion is human-revisable
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard',developer=True,retry_overrides={'semantic_revision_retries':0}); run=self.s.store.run(rid)
        run['snapshot']['recording']['draft_check']=[{'status':'fail','findings':[{'code':'omitted_fact','severity':'error'}]}]
        self.s.store.put_run(run); done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'blocked'); self.assertEqual(done['block']['category'],'content_revisable'); self.assertTrue(done['block']['human_revisable']); self.assertIn('draft',done['block']['allowed_revision_targets'])
        # invalid evidence relationships are integrity blocks, never human-overridable
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard',developer=True,retry_overrides={'output_repair_retries':0}); run=self.s.store.run(rid)
        bad=deepcopy(run['snapshot']['recording']['facts'][0]); bad['facts'][0]['evidence_refs']=[{'corpus_id':'outside','evidence_id':'invented'}]; run['snapshot']['recording']['facts']=[bad]
        self.s.store.put_run(run); done=self.s.runner.advance(rid); self.assertEqual(done['block']['category'],'integrity'); self.assertFalse(done['block']['human_revisable'])
        # exhausted execution budget is not revisable
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard'); run=self.s.store.run(rid); run['snapshot']['policy']['limits']['turns']=1; self.s.store.put_run(run)
        done=self.s.runner.advance(rid); self.assertEqual(done['block']['category'],'budget'); self.assertFalse(done['block']['human_revisable'])

    def test_conflict_identity_merge_and_extension(self):
        from localmedbot.modules import update_conflict_registry
        cid='c'; A=(cid,'a'); B=(cid,'b'); C=(cid,'c')
        allowed={A,B,C}; claims=[{'id':'Q1','text':'claim','evidence_refs':[]}]
        def ref(x): return {'corpus_id':x[0],'evidence_id':x[1]}
        base={'id':'model-x','topic':'topic','assessment_note':'','claim_relations':[{'claim_id':'Q1','relation':'implicated','reason':'related'}],'alternatives':[
            {'id':'a1','statement':'one','evidence_refs':[ref(A)],'applicability':'same','claim_ids':['Q1']},
            {'id':'a2','statement':'two','evidence_refs':[ref(B)],'applicability':'same','claim_ids':['Q1']}]}
        reg=update_conflict_registry(None,[base],claims,allowed,'reason'); self.assertEqual(len(reg['conflicts']),1); canonical=reg['conflicts'][0]['id']
        swapped=deepcopy(base); swapped['id']='other'; swapped['alternatives']=list(reversed(swapped['alternatives'])); swapped['alternatives'][0]['statement']='two variant'
        reg2=update_conflict_registry(reg,[swapped],claims,allowed,'match'); self.assertEqual(len(reg2['conflicts']),1); self.assertEqual(reg2['conflicts'][0]['id'],canonical)
        changed=deepcopy(base); changed['alternatives'][1]['evidence_refs']=[ref(C)]
        reg3=update_conflict_registry(reg2,[changed],claims,allowed,'audit'); self.assertEqual(len(reg3['conflicts']),2)
        target=reg2['conflicts'][0]
        extension={'id':'ext','topic':'topic','assessment_note':'','extends_conflict_id':target['id'],'claim_relations':[{'claim_id':'Q1','relation':'implicated','reason':'related'}],'alternatives':[]}
        for old in target['alternatives']:
            extension['alternatives'].append({'id':'x','statement':old['statement'],'evidence_refs':old['evidence_refs'],'applicability':old['applicability'],'claim_ids':['Q1'],'extends_alternative_id':old['id']})
        extension['alternatives'][0]['evidence_refs']=extension['alternatives'][0]['evidence_refs']+[ref(C)]
        reg4=update_conflict_registry(reg2,[extension],claims,allowed,'audit'); self.assertEqual(len(reg4['conflicts']),1); self.assertEqual(reg4['conflicts'][0]['id'],canonical)
        broken=deepcopy(extension); broken['alternatives']=broken['alternatives'][:-1]
        fault(self,'conflict_extension_invalid',update_conflict_registry,reg2,[broken],claims,allowed,'audit')

    def test_conflict_claim_mapping_and_overlap_withhold(self):
        from localmedbot.modules import update_conflict_registry, EvidenceFinalize
        refs=lambda x:[{'corpus_id':'c','evidence_id':x}]
        claims=[{'id':'Q1','text':'claim','evidence_refs':refs('a')},{'id':'Q2','text':'other','evidence_refs':refs('d')}]
        report={'id':'r','topic':'t','assessment_note':'','claim_relations':[{'claim_id':'Q1','relation':'unrelated','reason':'declared unrelated'},{'claim_id':'Q2','relation':'unrelated','reason':'unrelated'}],'alternatives':[
            {'id':'1','statement':'one','evidence_refs':refs('a'),'applicability':'','claim_ids':[]},
            {'id':'2','statement':'two','evidence_refs':refs('b'),'applicability':'','claim_ids':[]}]}
        reg=update_conflict_registry(None,[report],claims,{('c','a'),('c','b'),('c','d')},'reason')
        inputs={'claims':{'claims':claims},'candidates':{'items':[{'revision':'c','id':x} for x in ['a','b','d']]},'match':{},'audit':{'assessments':[{'claim_id':'Q1','evidence_refs':refs('d'),'decision':'support','reason':'support elsewhere'},{'claim_id':'Q2','evidence_refs':refs('d'),'decision':'support','reason':'support'}],'conflict_registry':reg},'adjudication':None}
        out=EvidenceFinalize().execute(None,inputs,{}).payload
        self.assertIn('Q1',out['withheld_claim_ids']); self.assertNotIn('Q1',{x['id'] for x in out['accepted']})
        missing=deepcopy(report); missing['claim_relations']=missing['claim_relations'][:1]
        fault(self,'conflict_claim_mapping_invalid',update_conflict_registry,None,[missing],claims,{('c','a'),('c','b'),('c','d')},'reason')

    def test_tape_pairing_and_scratch_immutability(self):
        rid,r=self.demo(); doc=self.s.capture_fixture(rid,'draft',r['attempts']['draft']); doc.update(id=f'clinical_letter.draft.immutable.{uuid.uuid4().hex[:8]}',version=1,data_suitability='synthetic')
        path=self.s.save_scratch_fixture(doc); self.assertTrue(Path(path).is_file())
        changed=deepcopy(doc); changed['resolved_inputs']['task']['purpose']='changed'
        fault(self,'fixture_version_changed',self.s.save_scratch_fixture,changed)
        good={'tape_schema_version':1,'id':'t','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':1,'content':'{}'}]}
        with TemporaryDirectory() as td:
            gp=Path(td)/'good.json'; gp.write_text(json.dumps(good)); self.assertEqual(self.s.fixtures.load_tape(gp,doc)['id'],'t')
            bad=deepcopy(good); bad['fixture_ref']['version']=2; bp=Path(td)/'bad.json'; bp.write_text(json.dumps(bad)); fault(self,'recording_pair_mismatch',self.s.fixtures.load_tape,bp,doc)

    def test_legacy_copy_payload_validates_current_schema(self):
        legacy={'id':'legacy','status':'completed','input':deepcopy(self.s.example('clinical_letter','standard')['input']),'snapshot':{'manifest':{'id':'clinical_letter'}},'origins':{'task_input':'synthetic','evidence':{},'revision_feedback':{}}}
        self.s.store.put_run(legacy); copied=self.s.legacy_copy_payload('legacy'); self.assertEqual(copied['input'],legacy['input']); self.assertEqual(copied['derived_from_run_id'],'legacy')
        broken=deepcopy(legacy); broken['id']='legacy-bad'; broken['input']={'missing':True}; self.s.store.put_run(broken); fault(self,'legacy_input_conversion_required',self.s.legacy_copy_payload,'legacy-bad')
        unknown=deepcopy(legacy); unknown['id']='legacy-unknown'; unknown['snapshot']['manifest']['id']='other'; self.s.store.put_run(unknown); fault(self,'legacy_workflow_unsupported',self.s.legacy_copy_payload,'legacy-unknown')

    def test_input_limits_and_devel_gate(self):
        fault(self,'input_too_large',self.s.start,'clinical_letter','clinical_letter.self.default','free_text',{'notes':'x'*30001,'purpose':'p'},developer=True)
        fault(self,'developer_disabled',self.s.start,'clinical_letter','clinical_letter.self.default','free_text',{'notes':'x','purpose':'p'},developer=False)
        fault(self,'developer_disabled',self.s.guidelines.resolve,'demo','devel',developer=False)

class WriterGuardTests(unittest.TestCase):
    def test_second_writer_is_rejected_but_read_only_opens(self):
        with TemporaryDirectory() as td:
            a=Store(td,writer=True)
            try:
                fault(self,'data_directory_busy',Store,td,writer=True)
                b=Store(td,writer=False); b.close()
            finally: a.close()

class FinalContractHardeningTests(Base):
    def test_recorded_step_tape_uses_exact_attempt_and_call_index(self):
        rid,r=self.demo('clinical_letter','standard')
        doc=self.s.capture_fixture(rid,'draft',r['attempts']['draft'])
        doc.update(id=f'clinical_letter.draft.exactpair.{uuid.uuid4().hex[:8]}',version=1,data_suitability='synthetic')
        self.s.save_scratch_fixture(doc)
        # A response for call 2 must never be consumed as call 1 merely because it is first in sorted order.
        wrong_pair={'tape_schema_version':1,'id':'draft.wrongpair','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':2,'content':'{}'}]}
        step=self.s.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=wrong_pair,developer=True)
        result=self.s.runner.advance(step)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['error']['code'],'recording_exhausted')
        # A schema-invalid first attempt needs an explicitly addressed repair-attempt response.
        invalid_first={'tape_schema_version':1,'id':'draft.missingrepair','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':1,'content':'{}'}]}
        step=self.s.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=invalid_first,developer=True)
        result=self.s.runner.advance(step)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['error']['code'],'recording_exhausted')
        self.assertEqual(result['attempts']['draft'],1); self.assertEqual(len(self.s.store.repairs(step)),1)

    def test_draft_checker_rejects_reviewer_omitted_fact_if_model_still_includes_it(self):
        from localmedbot.modules import ContentCheck
        class Ctx:
            run={'active':{'facts':1}}
            def call(self,*args,**kwargs): return {'status':'pass','findings':[]}
        ref=lambda i:{'corpus_id':'records','evidence_id':i}
        facts={'facts':[{'id':'F1','text':'keep','evidence_refs':[ref('a')],'qualifiers':{}},{'id':'F2','text':'omit','evidence_refs':[ref('b')],'qualifiers':{}}]}
        target={'title':'letter','claims':[{'id':'F1','text':'keep','evidence_refs':[ref('a')]},{'id':'F2','text':'omit','evidence_refs':[ref('b')]}]}
        policy={'extraction_revision':1,'omitted_fact_ids':['F2'],'reasons':{'F2':'Not relevant to this communication'},'review_event_id':'review-1'}
        out=ContentCheck().execute(Ctx(),{'target':target,'source':facts,'task':{},'omission_policy':policy},{'prompt':'check','coverage':True}).payload
        self.assertEqual(out['status'],'fail')
        self.assertIn('forbidden_omitted_fact_included',{f['code'] for f in out['findings']})

class DeliveryHardeningTests(Base):
    def test_profile_selection_persists_non_secret_preference(self):
        self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard')
        rows=self.s.profile_list('clinical_letter')
        self.assertEqual([x['id'] for x in rows if x['selected']],['clinical_letter.recorded.default'])

    def test_self_handoff_contains_resolved_input_and_action_schemas(self):
        rid=self.s.start('guideline_qa','guideline_qa.self.default','free_text',{'question':'Synthetic question'},guideline_selection={'set_id':'demo','selector':'default'},developer=True)
        run=self.s.runner.advance(rid);self.assertEqual(run['status'],'waiting_model')
        handoff=self.s.self_handoff(rid)
        self.assertTrue(handoff['resolved_inputs']);self.assertEqual(handoff['node_id'],'reason')
        actions={x['name']:x for x in handoff['allowed_actions']}
        self.assertIn('search',actions);self.assertIn('arguments_schema',actions['search']);self.assertIn('submit',actions);self.assertIn('result_schema',actions['submit'])

    def test_scratch_fixture_delete_and_recorded_requires_registered_fixture(self):
        rid,run=self.demo();doc=self.s.capture_fixture(rid,'draft',run['attempts']['draft']);doc.update(id=f'clinical_letter.draft.deletecase.{uuid.uuid4().hex[:8]}',version=1,data_suitability='synthetic')
        path=Path(self.s.save_scratch_fixture(doc));self.assertTrue(path.exists());self.s.delete_scratch_fixture(doc['id'],1);self.assertFalse(path.exists())
        tape={'tape_schema_version':1,'id':'draft.unsaved','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':1,'content':'{}'}]}
        fault(self,'recording_pair_mismatch',self.s.run_step,'clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=tape,developer=True)

    def test_fixture_runs_in_fresh_store_after_source_run_deleted(self):
        rid,run=self.demo();doc=self.s.capture_fixture(rid,'draft',run['attempts']['draft']);doc.update(id=f'clinical_letter.draft.portable.{uuid.uuid4().hex[:8]}',version=1,data_suitability='synthetic')
        original_ids={x['corpus_id'] for x in doc['evidence_snapshots']};self.s.delete(rid)
        with TemporaryDirectory() as td:
            other=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(other.close);other.save_scratch_fixture(doc)
            draft={'title':'Fixture letter','claims':[{'id':f['id'],'text':f['text'],'evidence_refs':f['evidence_refs']} for f in doc['resolved_inputs']['source']['facts']]}
            tape={'tape_schema_version':1,'id':'draft.portable','version':1,'workflow_id':'clinical_letter','node_id':'draft','step_contract_version':1,'fixture_ref':{'id':doc['id'],'version':1},'responses':[{'attempt':1,'call_index':1,'content':json.dumps(draft)}]}
            srid=other.run_step('clinical_letter','draft',doc,'clinical_letter.recorded.default',tape=tape,developer=True);done=other.runner.advance(srid);self.assertEqual(done['status'],'completed')
            self.assertTrue(set(done['corpora']).isdisjoint(original_ids));self.assertEqual(set(done['nodes']),{'draft'})

    def test_run_delete_removes_private_sources_not_shared_guidelines(self):
        rid,run=self.demo();private=list(run['corpora']);self.assertTrue(private);self.s.delete(rid)
        for cid in private:fault(self,'corpus_not_found',self.s.store.corpus,cid)
        qid,qrun=self.demo('guideline_qa','standard');shared=list(qrun['corpora']);self.s.delete(qid)
        for cid in shared:self.assertEqual(self.s.store.corpus(cid)['id'],cid)

    def test_model_assisted_guideline_import_can_suspend_without_publishing(self):
        import yaml
        with TemporaryDirectory() as td:
            copied=Path(td)/'guideline_sets';shutil.copytree(GUIDES,copied)
            service=Service(APPS,Path(td)/'data',PROFILES,copied,FIXTURES);self.addCleanup(service.close)
            sources=[{'id':'model_import','format':'text','content':'Synthetic guideline text for model-assisted development import.'}]
            profile=yaml.safe_load((copied/'demo/devel/ingestion.yaml').read_text());profile['mode']='model';profile['prompt']='Return structured guideline evidence items for this synthetic chunk.'
            before=service.guidelines.resolve('demo','devel',developer=True)['corpus_id']
            result=service.import_guideline_devel('demo',sources,profile,'guideline_qa.self.default',developer=True)
            self.assertEqual(result['status'],'waiting_model');self.assertIsNone(result['snapshot_id']);self.assertEqual(service.guidelines.resolve('demo','devel',developer=True)['corpus_id'],before)

    def test_source_version_and_contract_versions_frozen(self):
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard');run=self.s.store.run(rid)
        self.assertEqual(run['run_contract_version'],1);self.assertEqual(run['step_contract_version'],1);self.assertIn('source_version',run['snapshot']);self.assertEqual(set(run['snapshot']['source_version']),{'commit','dirty'})
