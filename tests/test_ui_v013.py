"""Focused document, workspace boundary and local launcher regression checks."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io,json,signal,socket,subprocess,time,unittest,urllib.error,urllib.request
from contextlib import redirect_stdout

from localmedbot import __version__
from localmedbot.cli import serve_local
from localmedbot.contracts import Fault
from localmedbot.modules import ContentCheck,_reason_contract_validator
from localmedbot.service import Service
from localmedbot.web import create_app

ROOT=Path(__file__).resolve().parents[1]

class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Service(ROOT/'applications',self.tmp.name,ROOT/'model_profiles',ROOT/'guideline_sets',ROOT/'tests/fixtures/steps');self.addCleanup(self.s.close)

    def test_configured_purposes_freeze_genre_and_reject_unknown(self):
        catalogue=self.s.load('clinical_letter')['purposes']
        displayed=next(a for a in self.s.applications() if a['id']=='clinical_letter')['purposes']
        self.assertEqual({x['id'] for x in displayed['options']},{x['id'] for x in catalogue['options']})
        self.assertIn(displayed['default'],{x['id'] for x in displayed['options']})
        for option in catalogue['options']:
            with self.subTest(purpose=option['id']):
                rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':option['id']},developer=True)
                run=self.s.store.run(rid)
                self.assertEqual(run['input']['purpose'],option['id'])
                self.assertEqual(run['snapshot']['selected_purpose'],option)
                draft=next(n for n in run['snapshot']['workflow']['nodes'] if n['id']=='draft')
                self.assertEqual(draft['inputs']['genre'],'run.snapshot.selected_purpose')
        with self.assertRaises(Fault) as raised:
            self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':'unknown'},developer=True)
        self.assertEqual(raised.exception.code,'invalid_purpose')

    def test_document_mapping_and_coverage_are_independent_of_prose_checker(self):
        a={'id':'F1','text':'Alpha','evidence_refs':[{'corpus_id':'record','evidence_id':'one'}],'qualifiers':{}}
        b={'id':'F2','text':'Beta','evidence_refs':[{'corpus_id':'record','evidence_id':'two'}],'qualifiers':{}}
        row={'passage':'Alpha and Beta','fact_ids':['F1','F2'],'evidence_refs':a['evidence_refs']+b['evidence_refs']}
        doc={'document':'Dear clinician, Alpha and Beta. Regards','provenance':[row]}
        source={'facts':[a,b]}
        self.assertEqual(_reason_contract_validator({'source':source})(doc),[])
        self.assertIn('contract.passage_missing',{f['code'] for f in _reason_contract_validator({'source':source})({**doc,'document':'Other'})})
        class Context:
            run={'active':{'facts':1}}
            def call(self,*args):return {'status':'fail','findings':[{'code':'unsupported_prose','severity':'error'}]}
        result=ContentCheck().execute(Context(),{'target':doc,'source':source,'task':{},'omission_policy':{}},{'document_coverage':True,'prompt':'test'}).payload
        self.assertEqual(result['status'],'fail')
        self.assertIn('unsupported_prose',{x['code'] for x in result['findings']})

    def test_different_genres_reach_draft_model_messages(self):
        for purpose in ('letter_to_gp','operation_report'):
            with self.subTest(purpose=purpose):
                rid=self.s.start('clinical_letter','clinical_letter.self.default','free_text',{'notes':'Synthetic note.','purpose':purpose},developer=True)
                # Upstream steps are satisfied through the bounded runtime, not
                # bypassed by a parallel drafting execution path.
                while True:
                    run=self.s.runner.advance(rid)
                    if run['status']!='waiting_model':break
                    handoff=self.s.self_handoff(rid)
                    if handoff['node_id']=='facts':
                        response={'facts':[{'id':'F1','text':'Synthetic note.','evidence_refs':[{'corpus_id':self.s.store.artifact(rid,'records')['payload']['corpus_id'],'evidence_id':self.s.store.artifact(rid,'records')['payload']['items'][0]['id']}],'qualifiers':{}}],'omission_suggestions':[]}
                    elif handoff['node_id']=='facts_check':response={'status':'pass','findings':[]}
                    else:break
                    self.s.self_submit(rid,{'contract_version':handoff['contract_version'],'request_id':handoff['request_id'],'content':json.dumps(response)})
                self.assertEqual(handoff['node_id'],'draft')
                self.assertEqual(handoff['resolved_inputs']['genre']['id'],purpose)
                selected=run['snapshot']['selected_purpose']
                self.assertEqual(handoff['resolved_inputs']['genre']['instructions'],selected['instructions'])

    def test_recorded_document_retains_provenance_without_citations(self):
        rid=self.s.start('clinical_letter','clinical_letter.recorded.default','demo',example='standard')
        run=self.s.runner.advance(rid)
        self.assertEqual(run['status'],'waiting_review')
        output=self.s.store.artifact(rid,'output')['payload'];draft=self.s.store.artifact(rid,'draft')['payload']
        self.assertEqual(output['text'],draft['document'])
        self.assertTrue(output['provenance']);self.assertFalse(output['citations'])
        self.assertNotIn('[1]',output['text'])
        other=self.s.start('guideline_qa','guideline_qa.recorded.default','demo',example='standard')
        qa=self.s.runner.advance(other)
        self.assertEqual(qa['status'],'waiting_review')
        self.assertTrue(self.s.store.artifact(other,'output')['payload']['citations'])

class LauncherTests(unittest.TestCase):
    def test_real_loopback_server_no_browser_and_http_failures(self):
        with TemporaryDirectory() as tmp:
            probe=socket.socket();probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
            command=[str(ROOT/'.env/bin/localmedbot'),'--data',tmp,'--runs-root',str(Path(tmp)/'runs'),'serve','--port',str(port),'--no-browser']
            # Background test launchers can inherit SIGINT ignored. Give the exec'd
            # server a normal interrupt disposition without changing the parent later.
            previous_interrupt=signal.getsignal(signal.SIGINT)
            try:
                signal.signal(signal.SIGINT,signal.default_int_handler)
                process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            finally:
                signal.signal(signal.SIGINT,previous_interrupt)
            try:
                for _ in range(80):
                    if process.poll() is not None:self.fail('Server exited before binding')
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=1) as response:self.assertEqual(response.status,200)
                        break
                    except (OSError,urllib.error.URLError):time.sleep(.1)
                else:self.fail('Server did not bind')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/session') as response:self.assertEqual(response.status,200)
                with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(f'http://127.0.0.1:{port}/not-a-route')
                self.assertEqual(failure.exception.code,404)
            finally:
                if process.poll() is None:process.send_signal(signal.SIGINT)
                try:output=process.communicate(timeout=8)[0]
                except subprocess.TimeoutExpired:process.kill();output=process.communicate()[0]
            self.assertEqual(process.returncode,0)
            self.assertIn(__version__,output)
            self.assertIn(f'http://127.0.0.1:{port}',output)
            self.assertNotIn(' 200 ',output)
            self.assertIn(' 404 ',output)

    def test_bind_launch_and_quiet_success_failure_logging(self):
        with TemporaryDirectory() as tmp:
            service=Service(ROOT/'applications',tmp,ROOT/'model_profiles',ROOT/'guideline_sets',ROOT/'tests/fixtures/steps')
            class StopServing(Exception):pass
            class Server:
                server_port=8765
                def serve_forever(self):raise StopServing()
                def server_close(self):pass
            class Pool:
                def shutdown(self,wait=True):pass
            class App:
                extensions={'localmedbot_pool':Pool()}
            events=[]
            def bound(host,port,app,threaded,request_handler):
                self.assertEqual(host,'127.0.0.1');events.append('bound')
                handler=request_handler.__new__(request_handler)
                with patch.object(request_handler.__bases__[0],'log_request') as log:
                    handler.log_request(200);handler.log_request(404)
                    self.assertEqual(log.call_count,1)
                return Server()
            def opened(url):events.append('browser');return True
            stream=io.StringIO()
            with patch('localmedbot.cli.create_app',create=True,return_value=App()),patch('werkzeug.serving.make_server',side_effect=bound),patch('localmedbot.cli.webbrowser.open',side_effect=opened),redirect_stdout(stream):
                with self.assertRaises(StopServing):serve_local(service)
            self.assertEqual(events,['bound','browser']);self.assertIn(__version__,stream.getvalue());self.assertIn('http://127.0.0.1:8765',stream.getvalue())
            events.clear()
            with patch('localmedbot.cli.create_app',create=True,return_value=App()),patch('werkzeug.serving.make_server',side_effect=bound),patch('localmedbot.cli.webbrowser.open',side_effect=opened):
                with self.assertRaises(StopServing):serve_local(service,open_browser=False)
            self.assertEqual(events,['bound']);service.close()

if __name__=='__main__':unittest.main()