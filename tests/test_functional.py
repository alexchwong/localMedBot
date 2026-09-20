"""Functional tests only: fixture inputs, state, types, IDs and relationships."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest
import yaml
from localmedbot.contracts import Fault, Module, ModuleResult, Registry, validate
from localmedbot.compiler import compile_workflow
from localmedbot.knowledge import Knowledge, prepare_sources, indexes_valid, source_passage
from localmedbot.modules import registry, CHECK_SCHEMA
from localmedbot.runtime import Runner
from localmedbot.service import Service
from localmedbot.storage import Store

APPS = Path(__file__).resolve().parents[1] / 'applications'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s=Service(APPS,self.tmp.name)

    def fault(self,code,fn,*args,**kwargs):
        with self.assertRaises(Fault) as error:fn(*args,**kwargs)
        self.assertEqual(error.exception.code,code)

    def started(self,app='clinical_letter',example='standard'):
        rid=self.s.start(app,example=example)
        return rid,self.s.store.run(rid)

    def completed(self,app='clinical_letter',example='standard'):
        rid,_=self.started(app,example)
        r=self.s.runner.advance(rid)
        self.assertEqual(r['status'],'waiting_review',r.get('error'))
        return rid,r


class Workflows(Base):
    def test_all_examples_run_and_approve(self):
        for app in self.s.applications():
            for example in app['examples']:
                with self.subTest(app=app['id'],example=example):
                    rid,r=self.completed(app['id'],example)
                    out=self.s.store.artifact(rid,'output')
                    self.assertTrue(out['payload']['synthetic'])
                    for c in out['payload']['citations']:
                        source=Knowledge(self.s.store,r['corpora']).read(c['corpus_id'],c['evidence_id'])
                        self.assertIn('passage',source['origin'])
                    approved=self.s.runner.review(rid,out['revision'],'fixture-reviewer','approve')
                    self.assertEqual(approved['status'],'completed')
                    self.assertEqual(approved['approval']['revision'],out['revision'])

    def test_supported_vs_unresolved_guideline(self):
        for example,expected in [('standard',1),('mismatch',0),('conflict',0),('no_evidence',0)]:
            rid,r=self.completed('guideline_qa',example)
            out=self.s.store.artifact(rid,'output')['payload']
            self.assertEqual(len(out['claim_ids']),expected)
            self.assertEqual(len(out['unresolved']),1-expected)
            self.assertEqual(r['nodes']['support.adjudicate'],'complete' if example in ['mismatch','conflict'] else 'skipped')

    def test_revision_invalidates_approval_and_preserves_ancestors(self):
        rid,r=self.completed()
        old=self.s.store.artifact(rid,'output')['revision']
        facts=r['active']['facts']
        self.s.runner.review(rid,old,'reviewer','approve')
        revised=self.s.runner.review(rid,old,'reviewer','revise','Clarify the communication purpose')
        self.assertIsNone(revised['approval'])
        self.assertNotIn('output',revised['active'])
        self.assertEqual(revised['active']['facts'],facts)
        r=self.s.runner.advance(rid)
        new=self.s.store.artifact(rid,'output')['revision']
        self.assertGreater(new,old)
        self.fault('stale_review',self.s.runner.review,rid,old,'reviewer','approve')
        self.assertEqual(self.s.runner.review(rid,new,'reviewer','approve')['status'],'completed')

    def test_failed_review_revises_then_passes(self):
        rid,r=self.started()
        r['snapshot']['recording']['draft_check']=[{'status':'fail','findings':[{'code':'omission'}]},{'status':'pass','findings':[]}]
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'waiting_review')
        self.assertEqual(done['cycles']['draft_check'],1)
        self.assertEqual(done['active']['draft'],2)
        self.assertEqual(done['active']['facts'],1)

    def test_repeated_review_failure_blocks(self):
        rid,r=self.started()
        r['snapshot']['recording']['draft_check']=[{'status':'fail','findings':[{'code':'omission'}]}]*2
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'blocked')
        self.assertEqual(done['error']['code'],'review_exhausted')
        self.assertNotIn('output',done['active'])

    def test_required_fact_omission_rejected_even_when_reviewer_passes(self):
        rid,r=self.started()
        r['snapshot']['recording']['facts'][0]['facts'].pop()
        r['snapshot']['recording']['facts']*=2
        r['snapshot']['recording']['facts_check']*=2
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'blocked')
        findings=self.s.store.artifact(rid,'facts_check')['payload']['findings']
        self.assertIn('omitted_fact',{f['code'] for f in findings})

    def test_unknown_source_reference_blocks(self):
        rid,r=self.started()
        r['snapshot']['recording']['facts'][0]['facts'][0]['evidence_ids']=['invented']
        r['snapshot']['recording']['facts']*=2;r['snapshot']['recording']['facts_check']*=2
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'blocked')
        self.assertIsNone(done['approval'])

    def test_syntax_and_schema_repairs(self):
        for invalid in ['{broken',{'wrong':True}]:
            with self.subTest(invalid=type(invalid).__name__):
                rid,r=self.started()
                valid=r['snapshot']['recording']['facts'][0]
                r['snapshot']['recording']['facts']=[invalid,valid]
                self.s.store.put_run(r)
                done=self.s.runner.advance(rid)
                self.assertEqual(done['status'],'waiting_review')
                self.assertEqual(done['repair_counts']['facts'],1)

    def test_repair_exhaustion(self):
        rid,r=self.started()
        r['snapshot']['recording']['facts']=['{','{']
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'blocked')
        self.assertEqual(done['error']['code'],'syntax_invalid')
        self.assertEqual(done['attempts']['facts'],2)

    def test_budgets_are_global_and_persist(self):
        for field,value in [('turns',1),('tokens',1),('node_turns',1),('tool_calls',1)]:
            with self.subTest(field=field):
                rid,r=self.started('guideline_qa')
                r['snapshot']['policy']['limits'][field]=value
                self.s.store.put_run(r)
                done=self.s.runner.advance(rid)
                self.assertEqual(done['status'],'blocked')
                self.assertEqual(done['error']['code'],'budget_exhausted')
                if not field.startswith('node_'):self.assertLessEqual(done['usage'][field],value)

    def test_tool_denial_and_cross_scope_arguments(self):
        for action,code in [({'action':'search','arguments':{'query':'Lumora','scope':'other'}},'tool_arguments'),({'action':'read','arguments':{'corpus_id':'outside','evidence_id':'x'}},'scope_denied')]:
            rid,r=self.started('guideline_qa')
            r['snapshot']['recording']['reason']=[action]
            self.s.store.put_run(r)
            self.assertEqual(self.s.runner.advance(rid)['error']['code'],code)
        rid,r=self.started('guideline_qa')
        r['snapshot']['policy']['tools']=[];self.s.store.put_run(r)
        self.assertEqual(self.s.runner.advance(rid)['error']['code'],'tool_denied')

    def test_agent_unknown_action_and_nontermination(self):
        for actions,code in [([{'action':'delete'}]*2,'schema_invalid'),([{'action':'search','arguments':{'query':'Lumora'}}]*6,'agent_turn_limit')]:
            rid,r=self.started('guideline_qa')
            r['snapshot']['recording']['reason']=actions;self.s.store.put_run(r)
            self.assertEqual(self.s.runner.advance(rid)['error']['code'],code)

    def test_invented_evidence_and_missing_assessment(self):
        for row in [{'assessments':[]},{'assessments':[{'claim_id':'C1','evidence_ids':['invented'],'decision':'support','reason':'fixture'}]}]:
            rid,r=self.started('guideline_qa')
            r['snapshot']['recording']['support.match']=[row,row];self.s.store.put_run(r)
            done=self.s.runner.advance(rid)
            self.assertEqual(done['error']['code'],'reference_invalid')
            self.assertNotIn('output',done['active'])

    def test_auditor_cannot_add_unassigned_support(self):
        rid,r=self.started('guideline_qa')
        row={'assessments':[{'claim_id':'C1','evidence_ids':['guide1:1:0'],'decision':'support','reason':'fixture'}]}
        r['snapshot']['recording']['support.audit']=[row,row];self.s.store.put_run(r)
        self.assertEqual(self.s.runner.advance(rid)['error']['code'],'reference_invalid')

    def test_snapshot_independent_of_later_assets(self):
        rid,r=self.started()
        original=r['snapshot']['workflow']['nodes'][1]['config']['prompt']
        with patch('localmedbot.compiler.asset',side_effect=AssertionError('unexpected asset read')):
            done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'waiting_review')
        self.assertTrue(original)

    def test_crash_before_commit_resumes_and_replays_response(self):
        rid,r=self.started()
        original=self.s.store.before_commit
        counter=[0]
        def crash():
            counter[0]+=1
            if counter[0]==2:raise RuntimeError('fixture crash')
        self.s.store.before_commit=crash
        with self.assertRaises(RuntimeError):self.s.runner.advance(rid)
        state=self.s.store.run(rid)
        self.assertIn('records',state['active']);self.assertNotIn('facts',state['active'])
        turns=state['usage']['turns']
        restarted=Service(APPS,self.tmp.name)
        done=restarted.runner.advance(rid)
        self.assertEqual(done['status'],'waiting_review')
        self.assertEqual(done['active']['records'],1)
        self.assertEqual(done['usage']['turns'],turns+3)
        kinds={e['kind'] for e in restarted.store.events(rid)}
        self.assertIn('response_replayed',kinds)
        self.assertIn('interrupted_attempt_resumed',kinds)

    def test_reject_and_cancel_do_not_approve(self):
        rid,r=self.completed()
        done=self.s.runner.review(rid,r['active']['output'],'reviewer','reject')
        self.assertEqual(done['status'],'blocked');self.assertIsNone(done['approval'])
        rid,r=self.started();self.s.runner.cancel(rid)
        self.assertEqual(self.s.runner.advance(rid)['status'],'cancelled')


class CompilerTests(Base):
    def test_configuration_variation_and_registered_subclass(self):
        class SpecialRenderer(self.s.registry.get('render').__class__):
            def execute(self,c,i,cfg):
                result=super().execute(c,i,cfg);result.payload['extension']=True;return result
        self.s.registry.register('custom_renderer',SpecialRenderer())
        snap=self.s.load('clinical_letter')
        # Avoid mutable filesystem dependencies by borrowing a fully resolved prepared snapshot.
        rid,r=self.started();snap=r['snapshot']
        for n in snap['workflow']['nodes']:
            if n['id']=='output':n['module']='custom_renderer';n['config']['template']='$body\n$title'
            if n['id']=='draft':n['config']['prompt']='Use the same source facts for a different communication purpose.'
        rid=self.s.runner.create(snap,r['input'],r['model'])
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'waiting_review')
        self.assertTrue(self.s.store.artifact(rid,'output')['payload']['extension'])

    def test_compile_rejects_invalid_graph_and_contracts(self):
        mutations=[('invalid_dependency_graph',lambda s:s['workflow']['nodes'][0].update(needs=['approval'])),
            ('unknown_module',lambda s:s['workflow']['nodes'][0].update(module='unregistered')),
            ('missing_producer',lambda s:s['workflow']['nodes'][0].update(inputs={'x':'artifacts.missing'})),
            ('missing_required_check',lambda s:s['policy']['required_checks'].append('missing')),
            ('duplicate_or_empty_nodes',lambda s:s['workflow']['nodes'].append(deepcopy(s['workflow']['nodes'][0])))]
        for code,mutate in mutations:
            snap=self.s.load('clinical_letter');mutate(snap)
            self.fault(code,compile_workflow,snap,self.s.registry)

    def test_skipped_required_input_is_not_success(self):
        rid,r=self.started()
        draft=next(n for n in r['snapshot']['workflow']['nodes'] if n['id']=='draft')
        draft['when']={'from':'artifacts.facts','op':'equals','value':False}
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['error']['code'],'required_input_missing')

    def test_registry_duplicate_and_schema_errors(self):
        self.fault('duplicate_module',self.s.registry.register,'render',self.s.registry.get('render'))
        self.fault('schema_invalid',validate,{'status':'unknown','findings':[]},CHECK_SCHEMA)


class KnowledgeTests(Base):
    def setUp(self):
        super().setUp()
        self.profile=yaml.safe_load((APPS/'guideline_qa/ingestion.yaml').read_text())
        self.sources=json.loads((APPS/'guideline_qa/corpus.json').read_text())

    def test_idempotent_import_and_atomic_failure(self):
        cid=self.s.ingest('guideline_qa')
        self.assertEqual(cid,self.s.ingest('guideline_qa'))
        bad=deepcopy(self.sources);bad[-1]['content']['items'][-1]['indexes']['priority']='wrong'
        self.fault('index_type',self.s.ingest,'guideline_qa',bad)
        self.assertEqual(self.s.store.active_corpus('guideline_qa','knowledge'),cid)
        changed=deepcopy(self.sources);changed[0]['content']['items'].pop()
        new=self.s.ingest('guideline_qa',changed)
        self.assertNotEqual(cid,new)
        self.assertEqual(len(self.s.store.corpus(cid)['items']),9)
        self.assertEqual(len(self.s.store.corpus(new)['items']),8)

    def test_query_filters_and_scope(self):
        cid=self.s.ingest('guideline_qa');k=Knowledge(self.s.store,[cid])
        results=k.search('Lumora',[{'field':'population','value':'child'}])
        self.assertEqual(len(results),1)
        self.assertEqual(len(k.search('',[{'field':'published','op':'gte','value':'2026-01-01'}])),3)
        self.assertEqual(len(k.search('',[{'field':'priority','op':'lte','value':1}])),3)
        self.assertEqual(k.search('unfindablefixture'),[])
        self.fault('unsupported_search',k.search,'',mode='semantic')
        self.fault('unsupported_operator',k.search,'',filters=[{'field':'population','op':'gte','value':'adult'}])
        self.fault('scope_denied',k.read,'other','guide1:0:0')

    def test_arbitrary_typed_indexes_and_cardinality(self):
        definitions={'department':{'type':'string','many':True},'flag':{'type':'boolean'},'age':{'type':'number'},'date':{'type':'date'}}
        indexes_valid({'department':['alpha','beta'],'flag':False,'age':4,'date':'2026-01-01'},definitions)
        self.fault('unknown_index',indexes_valid,{'gene':['x']},definitions)
        self.fault('index_cardinality',indexes_valid,{'department':'alpha'},definitions)
        self.fault('index_type',indexes_valid,{'age':True},definitions)
        self.fault('index_type',indexes_valid,{'date':'bad'},definitions)

    def test_text_partition_locators_and_model_extraction(self):
        p={**self.profile,'chunk_chars':5}
        sources=[{'id':'plain','format':'text','content':'abcdefghijk'}]
        rows=prepare_sources(sources,p)
        self.assertEqual(len(rows),3)
        cid=self.s.store.publish('fixture','x',p,sources,rows)
        corpus=self.s.store.corpus(cid)
        self.assertEqual(''.join(source_passage(corpus,r)['passage'] for r in rows),sources[0]['content'])
        p['mode']='model'
        generated=prepare_sources(sources,p,lambda chunk:[{'id':'x','text':chunk,'indexes':p['default_indexes']}])
        self.assertTrue(all(r['assertion_kind']=='extracted_assertion' for r in generated))

    def test_case_isolation_and_frozen_corpus_view(self):
        rid1,r1=self.completed();rid2,r2=self.completed()
        self.assertTrue(set(r1['corpora']).isdisjoint(r2['corpora']))
        self.fault('scope_denied',Knowledge(self.s.store,r1['corpora']).read,r2['corpora'][0],'record:0:0')
        rid,r=self.started('guideline_qa');old=r['corpora'][0]
        changed=deepcopy(self.sources);changed[0]['content']['items'].pop()
        self.s.ingest('guideline_qa',changed)
        self.assertEqual(self.s.runner.advance(rid)['corpora'],[old])

    def test_overlay_and_input_reordering(self):
        cid=self.s.ingest('guideline_qa')
        k=Knowledge(self.s.store,[cid],{'exclude':['guide1:0:0']})
        self.assertNotIn('guide1:0:0',{r['id'] for r in k.search('Lumora')})
        self.fault('evidence_not_found',k.read,cid,'guide1:0:0')
        rows=prepare_sources(list(reversed(self.sources)),self.profile)
        before=prepare_sources(self.sources,self.profile)
        self.assertEqual({x['id'] for x in rows},{x['id'] for x in before})


class AdditionalBoundaries(Base):
    def test_arbitrary_index_query_and_changed_schema(self):
        profile={'indexes':{'department':{'type':'string','many':True},'active':{'type':'boolean'}},'item_schema':{'type':'object','required':['id','text','indexes']},'formats':['json'],'mode':'direct'}
        sources=[{'id':'fixture','format':'json','content':{'items':[{'id':'a','text':'alpha case','indexes':{'department':['cardiology','clinic'],'active':False}}]}}]
        items=prepare_sources(sources,profile)
        cid=self.s.store.publish('fixture','fixture',profile,sources,items)
        result=Knowledge(self.s.store,[cid]).search('',[{'field':'department','op':'in','value':['clinic']},{'field':'active','value':False}])
        self.assertEqual(len(result),1)
        rid,r=self.started()
        n=next(n for n in r['snapshot']['workflow']['nodes'] if n['id']=='draft')
        n['schema']['required'].append('new_field')
        self.s.store.put_run(r)
        self.assertEqual(self.s.runner.advance(rid)['error']['code'],'schema_invalid')

    def test_runtime_model_ingestion_is_persisted_and_validated(self):
        rid,r=self.started()
        node=next(n for n in r['snapshot']['workflow']['nodes'] if n['id']=='records')
        node['config']['profile']['mode']='model'
        node['config']['profile']['prompt']='Extract factual items with source-preserving text and indexes.'
        r['input']['sources']=[{'id':'record','format':'text','content':'Synthetic fixture note'}]
        r['snapshot']['recording']['records']=[{'items':[{'id':'R1','text':'Synthetic fixture note','indexes':{'kind':'note'}}]}]
        # Only execute the ingestion node in this focused contract test.
        r['snapshot']['workflow']={'nodes':[node],'output':'records'}
        r['snapshot']['policy']['required_checks']=[];r['snapshot']['policy']['human_approval']=False
        r['nodes']={'records':'pending'}
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['status'],'completed')
        item=self.s.store.artifact(rid,'records')['payload']['items'][0]
        self.assertEqual(item['assertion_kind'],'extracted_assertion')
        self.assertEqual(item['locator'],'chars:0:22')  # fixture input locator, not generated prose
        self.assertTrue(any(e['kind']=='model_request' for e in self.s.store.events(rid)))

    def test_required_gate_cannot_be_conditionally_skipped(self):
        snap=self.s.load('clinical_letter')
        node=next(n for n in snap['workflow']['nodes'] if n['id']=='approval')
        node['when']={'from':'artifacts.output','op':'equals','value':False}
        self.fault('invalid_human_gate',compile_workflow,snap,self.s.registry)

    def test_elapsed_budget_exhaustion_blocks_release(self):
        rid,r=self.started()
        r['usage']['seconds']=r['snapshot']['policy']['limits']['seconds']+1
        self.s.store.put_run(r)
        done=self.s.runner.advance(rid)
        self.assertEqual(done['error']['code'],'budget_exhausted')
        self.assertIsNone(done['approval'])

    def test_unknown_tool_arguments_fail_cleanly(self):
        rid,r=self.started('guideline_qa')
        r['snapshot']['recording']['reason']=[{'action':'search','arguments':{'query':'Lumora','filters':'invalid'}}]
        self.s.store.put_run(r)
        self.assertEqual(self.s.runner.advance(rid)['error']['code'],'query_type')

    def test_crash_after_human_gate_commit_still_requires_approval(self):
        rid,_=self.started()
        original=self.s.store.commit
        def crash(run,node,payload,metadata):
            revision=original(run,node,payload,metadata)
            if node=='approval':raise RuntimeError('fixture post-commit interruption')
            return revision
        self.s.store.commit=crash
        with self.assertRaises(RuntimeError):self.s.runner.advance(rid)
        restarted=Service(APPS,self.tmp.name)
        state=restarted.runner.advance(rid)
        self.assertEqual(state['status'],'waiting_review')
        self.assertIsNone(state['approval'])

    def test_crash_after_failed_review_commit_cannot_skip_revision(self):
        rid,r=self.started()
        r['snapshot']['recording']['draft_check']=[{'status':'fail','findings':[{'code':'omission'}]},{'status':'pass','findings':[]}]
        self.s.store.put_run(r)
        original=self.s.store.commit
        def crash(run,node,payload,metadata):
            revision=original(run,node,payload,metadata)
            if node=='draft_check' and revision==1:raise RuntimeError('fixture post-commit interruption')
            return revision
        self.s.store.commit=crash
        with self.assertRaises(RuntimeError):self.s.runner.advance(rid)
        restarted=Service(APPS,self.tmp.name)
        state=restarted.runner.advance(rid)
        self.assertEqual(state['status'],'waiting_review')
        self.assertEqual(state['active']['draft'],2)
        self.assertEqual(state['cycles']['draft_check'],1)

    def test_agent_discovery_extends_initial_candidate_envelope(self):
        rid,r=self.started('guideline_qa')
        r['input']['question']='unmatched-initial-query'
        # Fixture agent searches the synonym; the initial retrieval is empty.
        self.s.store.put_run(r)
        state=self.s.runner.advance(rid)
        self.assertEqual(state['status'],'waiting_review')
        self.assertEqual(self.s.store.artifact(rid,'candidates')['payload']['items'],[])
        self.assertGreater(len(self.s.store.artifact(rid,'collected')['payload']['items']),0)
        self.assertEqual(len(self.s.store.artifact(rid,'output')['payload']['claim_ids']),1)
