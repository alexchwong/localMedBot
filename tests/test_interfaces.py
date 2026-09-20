"""Protocol and browser-service tests; no prose or HTML snapshots."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import json
import os
import subprocess
import sys
import time
import unittest
from localmedbot.contracts import Fault
from localmedbot.providers import HTTPProvider
from localmedbot.service import Service
from localmedbot.web import create_app

APPS=Path(__file__).resolve().parents[1]/'applications'


@contextmanager
def endpoint(responses):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append({'data':json.loads(self.rfile.read(int(self.headers['Content-Length']))),'authorization':self.headers.get('Authorization')})
            response=responses.pop(0)
            if isinstance(response,int):
                self.send_response(response);self.end_headers();return
            if isinstance(response,tuple):
                time.sleep(response[0]);response=response[1]
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            try:self.wfile.write(json.dumps(response).encode())
            except BrokenPipeError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',requests
    finally:server.shutdown();server.server_close();thread.join()


def reply(value):
    return {'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}],'usage':{'total_tokens':12}}


class HTTPTests(unittest.TestCase):
    def test_http_request_auth_and_response(self):
        with endpoint([reply({'ok':True})]) as (url,requests),patch.dict(os.environ,{'FIXTURE_KEY':'fixture-secret'}):
            provider=HTTPProvider({'base_url':url,'model':'fixture-model','api_key_env':'FIXTURE_KEY'})
            result=provider.complete({'messages':[{'role':'user','content':'fixture-input'}],'max_tokens':50,'timeout':1},'test',0)
            self.assertTrue(json.loads(result['text'])['ok'])
            self.assertEqual(result['usage']['total_tokens'],12)
            self.assertEqual(requests[0]['authorization'],'Bearer fixture-secret')
            self.assertEqual(requests[0]['data']['max_tokens'],50)

    def test_http_timeout_rejection_and_malformed_shape(self):
        for rows,expected,timeout in [([401],'provider_rejected',1),([429],'transport_retryable',1),([{}],'provider_shape',1),([(0.1,reply({}))],'transport_retryable',.01)]:
            with self.subTest(expected=expected),endpoint(rows) as (url,_):
                p=HTTPProvider({'base_url':url,'model':'fixture'})
                with self.assertRaises(Fault) as exc:p.complete({'messages':[],'max_tokens':5,'timeout':timeout},'x',0)
                self.assertEqual(exc.exception.code,expected)

    def test_http_pipeline_retry_and_secret_not_logged(self):
        with TemporaryDirectory() as tmp:
            demo=Service(APPS,tmp)
            fixture=demo.example('clinical_letter','standard')
            tape=fixture['responses']
            rows=[429]+[reply(tape[n][0]) for n in ['facts','facts_check','draft','draft_check']]
            with endpoint(rows) as (url,requests),patch.dict(os.environ,{'FIXTURE_KEY':'fixture-secret'}):
                service=Service(APPS,tmp,{'provider':'http','base_url':url,'model':'fixture-model','api_key_env':'FIXTURE_KEY'})
                rid=service.start('clinical_letter',fixture['input'])
                run=service.runner.advance(rid)
                self.assertEqual(run['status'],'waiting_review')
                self.assertEqual(run['usage']['turns'],5)
                self.assertEqual(len(requests),5)
                self.assertNotIn('fixture-secret',json.dumps(service.inspect(rid)))

    def test_model_credentials_must_not_be_inline(self):
        with TemporaryDirectory() as tmp:
            service=Service(APPS,tmp)
            snap=service.load('clinical_letter')
            with self.assertRaises(Fault) as error:service.runner.create(snap,service.example('clinical_letter','standard')['input'],{'provider':'recorded','api_key':'fixture-secret'})
            self.assertEqual(error.exception.code,'model_config_field')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=Service(APPS,self.tmp.name)
        self.app=create_app(self.service)
        self.addCleanup(self.app.extensions['localmedbot_pool'].shutdown)
        self.client=self.app.test_client()
        self.headers={'X-LocalMedBot-Token':self.app.extensions['localmedbot_token']}

    def post(self,path,data):return self.client.post(path,json=data,headers=self.headers)

    def test_browser_service_actions_sources_and_stale_review(self):
        rid=self.service.start('clinical_letter');run=self.service.runner.advance(rid)
        response=self.client.get('/api/runs/'+rid)
        self.assertEqual(response.status_code,200)
        data=response.get_json();out=data['artifacts']['output']
        citation=out['payload']['citations'][0]
        source=self.client.get(f"/api/runs/{rid}/source/{citation['corpus_id']}/{citation['evidence_id']}")
        self.assertEqual(source.status_code,200);self.assertIn('origin',source.get_json())
        payload={'revision':out['revision'],'actor':'fixture-reviewer','decision':'revise','comments':'fixture revision'}
        self.assertEqual(self.post('/api/runs/'+rid+'/review',payload).status_code,200)
        self.service.runner.advance(rid)
        payload['decision']='approve'
        stale=self.post('/api/runs/'+rid+'/review',payload)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(stale.get_json()['error']['code'],'stale_review')

    def test_origin_host_token_and_upload_validation(self):
        self.assertEqual(self.client.post('/api/runs',json={}).get_json()['error']['code'],'request_token')
        r=self.client.post('/api/runs',json={},headers={**self.headers,'Origin':'https://outside.invalid'})
        self.assertEqual(r.get_json()['error']['code'],'origin_denied')
        self.assertEqual(self.client.get('/',headers={'Host':'outside.invalid'}).get_json()['error']['code'],'host_denied')
        invalid=[{'id':'../../escape','format':'text','content':'fixture'}]
        r=self.post('/api/ingest/guideline_qa',{'sources':invalid})
        self.assertEqual(r.get_json()['error']['code'],'source_id')
        self.app.config['MAX_CONTENT_LENGTH']=100
        self.assertEqual(self.post('/api/ingest/guideline_qa',{'sources':['x'*200]}).status_code,413)

    def test_recorded_mode_rejects_custom_input(self):
        response=self.post('/api/runs',{'application':'clinical_letter','input':{'custom':True}})
        self.assertEqual(response.get_json()['error']['code'],'recorded_input_mismatch')

    def test_import_endpoint(self):
        r=self.post('/api/ingest/guideline_qa',{})
        self.assertEqual(r.status_code,200)
        self.assertEqual(len(self.service.store.corpus(r.get_json()['corpus_id'])['items']),9)


class CLITests(unittest.TestCase):
    def test_cli_workflow_and_exit_codes(self):
        with TemporaryDirectory() as tmp:
            base=[sys.executable,'-m','localmedbot.cli','--apps',str(APPS),'--data',tmp]
            run=subprocess.run(base+['run','clinical_letter'],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            doc=json.loads(run.stdout)
            self.assertEqual(doc['status'],'waiting_review')
            result=subprocess.run(base+['review',doc['id'],'--revision',str(doc['active']['output']),'--actor','fixture','--decision','approve'],capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout)['status'],'completed')
            bad=subprocess.run(base+['run','missing'],capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertEqual(json.loads(bad.stderr)['error']['code'],'application_not_found')
