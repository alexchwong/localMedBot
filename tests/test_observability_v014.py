"""Execution lifecycle and read-only projection regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import time
import unittest

from localmedbot.service import Service
from localmedbot.web import create_app

ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT/'applications'; PROFILES=ROOT/'model_profiles'; GUIDES=ROOT/'guideline_sets'; FIXTURES=ROOT/'tests/fixtures/steps'


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES); self.addCleanup(self.s.close)
        self.app=create_app(self.s); self.addCleanup(self.app.extensions['localmedbot_pool'].shutdown)
        self.client=self.app.test_client(); self.token=self.client.get('/api/session').get_json()['csrf']

    def post(self,path,data):
        return self.client.post(path,json=data,headers={'X-LocalMedBot-Token':self.token})

    def make_run(self):
        return self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard',title='Synthetic case')

    def test_worker_exception_is_persisted_with_explicit_recovery(self):
        with patch.object(self.s.runner,'advance',side_effect=RuntimeError('secret clinical payload')):
            # An execution started through HTTP must be observed even when advance raises.
            data={'workflow_id':'clinical_letter','profile_id':'clinical_letter.recorded.default','input_mode':'demo','example':'standard'}
            started=self.post('/api/runs',data); self.assertEqual(started.status_code,202)
            new_id=started.get_json()['id']
            for _ in range(200):
                observed=self.s.store.run(new_id)
                if observed['status']=='failed': break
                time.sleep(.01)
        self.assertEqual(observed['status'],'failed')
        self.assertEqual(observed['error']['code'],'execution_stopped')
        self.assertEqual(observed['error']['recovery_action'],'resume')
        self.assertNotIn('secret clinical payload',json.dumps(self.s.inspect(new_id)))
        with patch.object(self.s,'resume',return_value=observed) as resumed:
            self.assertEqual(self.post(f'/api/runs/{new_id}/resume',{}).status_code,202)
            for _ in range(200):
                if resumed.called: break
                time.sleep(.01)
        self.assertTrue(resumed.called)

    def test_unrecoverable_failed_run_is_not_resumable(self):
        rid=self.make_run(); row=self.s.store.run(rid)
        row['status']='failed'; row['error']={'code':'schema_invalid','stage':'facts','attempt':1}
        self.s.store.put_run(row)
        self.assertEqual(self.post(f'/api/runs/{rid}/resume',{}).status_code,409)
        self.assertEqual(self.s.store.run(rid)['status'],'failed')

    def test_worker_returns_active_state_is_reconciled(self):
        with patch.object(self.s.runner,'advance',side_effect=lambda rid:self.s.store.run(rid)):
            started=self.post('/api/runs',{'workflow_id':'clinical_letter','profile_id':'clinical_letter.recorded.default','input_mode':'demo','example':'standard'})
            self.assertEqual(started.status_code,202)
            rid=started.get_json()['id']
            for _ in range(200):
                row=self.s.store.run(rid)
                if row['status']=='failed': break
                time.sleep(.01)
        self.assertEqual(row['error']['code'],'execution_stopped')

    def test_pending_run_cannot_be_deleted_while_worker_is_queued(self):
        rid=self.make_run()
        self.assertEqual(self.client.delete(f'/api/runs/{rid}',headers={'X-LocalMedBot-Token':self.token}).status_code,409)
        self.assertEqual(self.s.store.run(rid)['status'],'pending')

    def test_startup_reconciles_pending_and_running_but_not_waiting(self):
        pending=self.make_run(); active=self.make_run(); waiting=self.make_run()
        row=self.s.store.run(active); row['status']='running'; row['nodes']['facts']='running'; row['attempts']['facts']=1; self.s.store.put_run(row)
        row=self.s.store.run(waiting); row['status']='waiting_model'; self.s.store.put_run(row)
        self.s.store.recover_startup()
        for rid in [pending,active]:
            run=self.s.store.run(rid)
            self.assertEqual(run['status'],'failed'); self.assertEqual(run['error']['recovery_action'],'resume')
            self.assertEqual(self.s.store.events(rid)[-1]['kind'],'execution_stopped')
        self.assertEqual(self.s.store.run(waiting)['status'],'waiting_model')
        with patch.object(self.s,'resume',return_value=self.s.store.run(active)) as resumed:
            self.assertEqual(self.post(f'/api/runs/{active}/resume',{}).status_code,202)
            for _ in range(200):
                if resumed.called: break
                time.sleep(.01)
        self.assertTrue(resumed.called)

    def test_uncertain_provider_requires_explicit_acknowledgement(self):
        rid=self.make_run(); row=self.s.store.run(rid); row['status']='running'; row['nodes']['facts']='running'; row['attempts']['facts']=1; self.s.store.put_run(row)
        with self.s.store.db() as db:
            doc={'request_id':'synthetic-request','run_id':rid,'node_id':'facts','attempt':1,'status':'dispatching'}
            db.execute('INSERT INTO model_calls(request_id,run_id,node_id,attempt,call_index,document) VALUES(?,?,?,?,?,?)',(doc['request_id'],rid,'facts',1,1,json.dumps(doc)))
        self.s.store.recover_startup(); run=self.s.store.run(rid)
        self.assertEqual(run['error']['code'],'external_response_unknown')
        self.assertEqual(self.s.store.model_call('synthetic-request')['status'],'dispatch_unknown')
        self.assertEqual(self.post(f'/api/runs/{rid}/resume',{}).status_code,400)
        self.assertEqual(self.s.store.run(rid)['status'],'failed')
        with patch.object(self.s,'resume',return_value=self.s.store.run(rid)) as resumed:
            self.assertEqual(self.post(f'/api/runs/{rid}/resume',{'acknowledge_external_retry':True}).status_code,202)
            for _ in range(200):
                if resumed.called: break
                time.sleep(.01)
        self.assertTrue(resumed.called)
        self.assertIs(resumed.call_args.kwargs.get('acknowledge_external_retry'),True)

    def test_projection_and_missing_artifact_are_truthful(self):
        rid=self.make_run(); before=self.s.inspect(rid)['execution']; self.assertEqual(before['reached_count'],0)
        self.s.runner.advance(rid); view=self.s.inspect(rid,developer=True)
        self.assertEqual(view['execution']['total'],sum(bool(n.get('model_dependent')) for n in view['run']['snapshot']['workflow']['nodes']))
        self.assertEqual(view['execution']['reached_count'],view['execution']['total'])
        self.assertTrue(view['execution']['timeline']); self.assertTrue(all(s['valid'] for s in view['execution']['stages']))
        public=self.s.inspect(rid)['execution']['timeline']
        self.assertTrue(all('resolved_inputs' not in a for a in public))
        self.assertTrue(all('messages' not in c for a in public for c in a['requests']))
        self.assertTrue(any('messages' in c for a in view['execution']['timeline'] for c in a['requests']))
        path=Path(view['artifacts']['facts']['path']); path.unlink()
        damaged=self.s.inspect(rid); self.assertTrue(damaged['inspection_degraded']); self.assertTrue(damaged['artifacts']['facts']['unavailable'])
        self.assertFalse(next(s for s in damaged['execution']['stages'] if s['id']=='facts')['valid'])

    def test_unissued_feedback_does_not_claim_dispatch(self):
        rid=self.make_run(); row=self.s.store.run(rid); row['status']='failed'; row['nodes']['facts']='failed'; self.s.store.put_run(row)
        repair={'repair_id':'synthetic-repair','node_id':'facts','attempt':1,'failed_call_id':'synthetic-failed','next_request_id':'synthetic-next','repair_ordinal':1,'frozen_limit':3,'outcome':'pending'}
        attempt={'run_id':rid,'node_id':'facts','attempt':1,'state':'failed','started':1.0}
        with self.s.store.db() as db:
            db.execute('INSERT INTO step_attempts(run_id,node_id,attempt,document) VALUES(?,?,?,?)',(rid,'facts',1,json.dumps(attempt)))
            db.execute('INSERT INTO repairs(repair_id,run_id,node_id,attempt,operation_id,document) VALUES(?,?,?,?,?,?)',('synthetic-repair',rid,'facts',1,'synthetic-operation',json.dumps(repair)))
        view=self.s.inspect(rid,developer=True)
        self.assertFalse(view['execution']['timeline'][0]['repairs'][0]['feedback_issued'])
        self.assertEqual(view['execution']['reached_count'],1)

    def test_ordered_titles_and_invalid_title(self):
        first=self.make_run(); second=self.make_run()
        rows=self.client.get('/api/runs').get_json()
        self.assertEqual([r['id'] for r in rows[:2]],[second,first]); self.assertEqual(rows[0]['title'],'Synthetic case')
        self.assertEqual(self.client.get('/api/runs?workflow_id=absent').get_json(),[])
        invalid={'workflow_id':'clinical_letter','profile_id':'clinical_letter.recorded.default','input_mode':'demo','example':'standard','title':'secret\nnewline'}
        self.assertEqual(self.post('/api/runs',invalid).status_code,400)
        self.s.vault.set('synthetic-profile','synthetic-private-key')
        invalid['title']='synthetic-private-key'
        self.assertEqual(self.post('/api/runs',invalid).status_code,400)


if __name__=='__main__': unittest.main()