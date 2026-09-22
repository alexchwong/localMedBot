"""Regression coverage for pre-0.1.0 engine capabilities under v2 contracts.

Assertions target state, types, IDs, source relationships and versioned fixtures;
they intentionally avoid production prose snapshots.
"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json,unittest,yaml

from localmedbot.contracts import Fault,validate
from localmedbot.compiler import compile_workflow
from localmedbot.knowledge import Knowledge,indexes_valid,prepare_sources,source_passage
from localmedbot.modules import registry,CHECK_SCHEMA
from localmedbot.service import Service

ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT/'applications'; PROFILES=ROOT/'model_profiles'; GUIDES=ROOT/'guideline_sets'; FIXTURES=ROOT/'tests/fixtures/steps'

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES);self.addCleanup(self.s.close)
    def fault(self,code,fn,*a,**kw):
        with self.assertRaises(Fault) as e:fn(*a,**kw)
        self.assertEqual(e.exception.code,code)
    def demo(self,app='clinical_letter',example='standard'):
        rid=self.s.start(app,f'{app}.recorded.default','demo',example=example)
        return rid,self.s.runner.advance(rid)

class WorkflowRegressionTests(Base):
    def test_all_versioned_examples_reach_review(self):
        for app in self.s.applications():
            for example in app.get('examples',[]):
                with self.subTest(app=app['id'],example=example):
                    rid,run=self.demo(app['id'],example)
                    self.assertEqual(run['status'],'waiting_review',run.get('error'))
                    out=self.s.store.artifact(rid,run['snapshot']['workflow']['output'])['payload']
                    for c in out.get('citations',[]):
                        src=Knowledge(self.s.store,run['corpora']).read(c['corpus_id'],c['evidence_id'])
                        self.assertIn('origin',src)
    def test_revision_invalidates_descendants_not_ancestors(self):
        rid,run=self.demo();old=run['active']['output'];facts=run['active']['facts']
        approved=self.s.review(rid,{'review_request_id':'11111111-1111-4111-8111-111111111111','revision':old,'actor':'Fixture reviewer','decision':'approve','comments':'','acknowledged_omission_ids':[],'acknowledged_conflict_ids':[]})
        self.assertEqual(approved['status'],'completed')
        revised=self.s.review(rid,{'review_request_id':'22222222-2222-4222-8222-222222222222','revision':old,'actor':'Fixture reviewer','decision':'revise','comments':'fixture feedback','target':'draft'})
        self.assertEqual(revised['active']['facts'],facts);self.assertNotIn('output',revised['active']);self.assertIsNone(revised['approval'])
        rerun=self.s.runner.advance(rid);self.assertEqual(rerun['status'],'waiting_review');self.assertGreater(rerun['active']['output'],old)
    def test_schema_and_reference_failures_do_not_release(self):
        rid,run=self.demo();self.assertEqual(run['status'],'waiting_review')
        # New run with deliberately schema-invalid controlled response fixture.
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard',developer=True,retry_overrides={'output_repair_retries':0});r=self.s.store.run(rid)
        r['snapshot']['recording']['facts']=[{'wrong':True},{'wrong':True}];self.s.store.put_run(r)
        done=self.s.runner.advance(rid);self.assertEqual(done['status'],'blocked');self.assertEqual(done['block']['category'],'integrity');self.assertIsNone(done['approval'])
    def test_tool_scope_and_denial_are_preserved(self):
        rid=self.s.start('guideline_qa','guideline_qa.recorded.default','demo',example='standard',developer=True,retry_overrides={'output_repair_retries':0});r=self.s.store.run(rid)
        r['snapshot']['recording']['reason']=[{'action':'read','arguments':{'corpus_id':'outside','evidence_id':'x'}}];self.s.store.put_run(r)
        done=self.s.runner.advance(rid);self.assertEqual(done['status'],'blocked');self.assertEqual(done['error']['code'],'protocol_invalid')
        self.assertEqual(self.s.store.tool_calls(rid),[])
    def test_budget_exhaustion_is_durable_non_revisable_block(self):
        rid=self.s.start('guideline_qa','guideline_qa.recorded.default','demo',example='standard');r=self.s.store.run(rid);r['snapshot']['policy']['limits']['turns']=1;self.s.store.put_run(r)
        done=self.s.runner.advance(rid);self.assertEqual(done['block']['category'],'budget');self.assertFalse(done['block']['human_revisable'])

class CompilerRegressionTests(Base):
    def test_invalid_graph_module_and_required_gate_rejected(self):
        cases=[]
        snap=self.s.load('clinical_letter');snap['workflow']['nodes'][0]['needs']=['approval'];cases.append(('invalid_dependency_graph',snap))
        snap=self.s.load('clinical_letter');snap['workflow']['nodes'][0]['module']='missing';cases.append(('unknown_module',snap))
        snap=self.s.load('clinical_letter');snap['policy']['required_checks'].append('missing');cases.append(('missing_required_check',snap))
        for code,snap in cases:
            with self.subTest(code=code):self.fault(code,compile_workflow,snap,self.s.registry)
    def test_node_input_schema_is_enforced(self):
        snap=self.s.load('clinical_letter');node=next(n for n in snap['workflow']['nodes'] if n['id']=='draft')
        self.fault('schema_invalid',validate,{'source':{}},node['input_schema'])
        validate({'status':'pass','findings':[]},CHECK_SCHEMA)

class KnowledgeRegressionTests(Base):
    def setUp(self):
        super().setUp();self.profile=yaml.safe_load((APPS/'guideline_qa/ingestion.yaml').read_text());self.sources=json.loads((GUIDES/'demo/releases/v1/sources.json').read_text())
    def test_direct_import_and_typed_filtering(self):
        items=prepare_sources(self.sources,self.profile);cid=self.s.store.publish('fixture','knowledge',self.profile,self.sources,items)
        k=Knowledge(self.s.store,[cid]);self.assertTrue(k.search('Lumora'));self.assertEqual(k.search('unfindablefixture'),[])
        self.assertTrue(k.search('',[{'field':'published','op':'gte','value':'2026-01-01'}]))
        self.fault('scope_denied',k.read,'other',items[0]['id'])
    def test_arbitrary_index_types_and_cardinality(self):
        definitions={'department':{'type':'string','many':True},'flag':{'type':'boolean'},'age':{'type':'number'},'date':{'type':'date'}}
        indexes_valid({'department':['clinic'],'flag':False,'age':4,'date':'2026-01-01'},definitions)
        self.fault('index_cardinality',indexes_valid,{'department':'clinic'},definitions);self.fault('index_type',indexes_valid,{'age':True},definitions)
    def test_text_partition_source_offsets_preserved(self):
        p=deepcopy(self.profile);p['chunk_chars']=5;sources=[{'id':'plain','format':'text','content':'abcdefghijk'}]
        rows=prepare_sources(sources,p);cid=self.s.store.publish('fixture','text',p,sources,rows);corpus=self.s.store.corpus(cid)
        self.assertEqual(''.join(source_passage(corpus,r)['passage'] for r in rows),sources[0]['content'])

if __name__=='__main__':unittest.main()
