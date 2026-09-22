"""Transport, CLI and optional Flask interface regressions for 0.1.0."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import importlib.util,json,os,subprocess,sys,time,unittest

from localmedbot.contracts import Fault
from localmedbot.providers import HTTPProvider
from localmedbot.service import Service

ROOT=Path(__file__).resolve().parents[1];APPS=ROOT/'applications';PROFILES=ROOT/'model_profiles';GUIDES=ROOT/'guideline_sets';FIXTURES=ROOT/'tests/fixtures/steps'

@contextmanager
def endpoint(responses):
    requests=[]
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            requests.append({'path':self.path,'body':json.loads(self.rfile.read(int(self.headers['Content-Length']))),'authorization':self.headers.get('Authorization')})
            item=responses.pop(0)
            if callable(item): item=item(requests[-1]['body'])
            if isinstance(item,int):self.send_response(item);self.end_headers();return
            if isinstance(item,tuple) and len(item)==2 and isinstance(item[0],int):
                status,payload=item;self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(payload).encode());return
            if isinstance(item,tuple):time.sleep(item[0]);item=item[1]
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            try:self.wfile.write(json.dumps(item).encode())
            except BrokenPipeError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),H);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',requests
    finally:server.shutdown();server.server_close();thread.join()

def reply(value):return {'model':'fixture-model','choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}],'usage':{'total_tokens':9}}
def responses_reply(value):return {'id':'fixture-response','model':'fixture-model','status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}],'usage':{'input_tokens':4,'output_tokens':5,'total_tokens':9}}
def responses_raw(text):
    result=responses_reply(None)
    result['output'][0]['content'][0]['text']=text
    return result
def native_reply(value):return {'response_id':'fixture-native','model':'fixture-model','output':[{'type':'message','content':json.dumps(value)}],'stats':{'input_tokens':4,'total_output_tokens':5,'reasoning_output_tokens':1}}

class HTTPRegressionTests(unittest.TestCase):
    def test_auth_shape_and_redirect_rejection(self):
        with endpoint([responses_reply({'ok':True})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'default'}
            p=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},credential='fixture-secret')
            out=p.complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertTrue(json.loads(out['text'])['ok']);self.assertEqual(requests[0]['authorization'],'Bearer fixture-secret')
            self.assertTrue(requests[0]['path'].endswith('/responses'));self.assertNotIn('response_format',requests[0]['body'])
        with endpoint([401]) as (url,_):
            settings={'model':'fixture','temperature':0,'max_tokens':5,'timeout_seconds':1,'reasoning':'default'}
            p=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},credential=None)
            with self.assertRaises(Fault) as e:p.complete({'role':'reasoning','messages':[],'timeout':1},'x',0)
            self.assertEqual(e.exception.code,'authentication_rejected')

    def test_lmstudio_reasoning_transports_and_fallback(self):
        with endpoint([responses_reply({'ok':True})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'high'}
            out=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},None).complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertTrue(json.loads(out['text'])['ok']);self.assertEqual(requests[0]['body']['reasoning'],{'effort':'high'})
        with endpoint([native_reply({'ok':True})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'none'}
            out=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},None).complete({'role':'reasoning','messages':[{'role':'system','content':'system'},{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertTrue(json.loads(out['text'])['ok']);self.assertTrue(requests[0]['path'].endswith('/api/v1/chat'));self.assertEqual(requests[0]['body']['reasoning'],'off')
        with endpoint([(404,{'error':'missing'}),reply({'ok':True})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'default'}
            out=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},None).complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertTrue(json.loads(out['text'])['ok']);self.assertEqual(len(requests),2);self.assertTrue(requests[1]['path'].endswith('/chat/completions'))

    def test_lmstudio_explicit_reasoning_never_silently_falls_back(self):
        with endpoint([(404,{'error':'missing'})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'medium'}
            p=HTTPProvider({'executor':'lmstudio','base_url':url,'effective_roles':{'reasoning':settings}},None)
            with self.assertRaises(Fault) as e:p.complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertEqual(e.exception.code,'provider_request_rejected');self.assertEqual(len(requests),1)

    def test_openrouter_reasoning_and_provider_error_detail(self):
        with endpoint([reply({'ok':True})]) as (url,requests):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'minimal'}
            out=HTTPProvider({'executor':'openrouter','base_url':url,'effective_roles':{'reasoning':settings}},'fixture-secret').complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertTrue(json.loads(out['text'])['ok']);self.assertEqual(requests[0]['body']['reasoning'],{'effort':'minimal'});self.assertNotIn('response_format',requests[0]['body'])
        with endpoint([(400,{'error':{'message':'fixture-secret fixture rejection reason'}})]) as (url,_):
            settings={'model':'fixture','temperature':0,'max_tokens':50,'timeout_seconds':1,'reasoning':'default'}
            p=HTTPProvider({'executor':'openrouter','base_url':url,'effective_roles':{'reasoning':settings}},'fixture-secret')
            with self.assertRaises(Fault) as e:p.complete({'role':'reasoning','messages':[{'role':'user','content':'fixture'}],'timeout':1},'x',0)
            self.assertEqual(e.exception.code,'provider_request_rejected');self.assertIn('fixture rejection reason',e.exception.detail);self.assertNotIn('fixture-secret',e.exception.detail)

    def test_provider_verification_uses_selected_lmstudio_reasoning_transport(self):
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(s.close)
            with endpoint([responses_reply({'action':'submit','result':{'ok':True}}) for _ in range(3)]) as (url,requests):
                pid='clinical_letter.lmstudio.default'
                s.configure_profile(pid,{'base_url':url,'model':'fixture','settings':{'reasoning':'high'}})
                result=s.verify_provider(pid,'clinical_letter')
                self.assertTrue(result['success'],result);self.assertTrue(requests)
                self.assertTrue(all(row['path'].endswith('/responses') for row in requests))
                self.assertTrue(all(row['body'].get('reasoning')=={'effort':'high'} for row in requests))
                self.assertTrue(all(row['attempts']==1 and row['repairs_used']==0 for row in result['results']))

    def test_lmstudio_verification_repairs_syntax_and_schema_output(self):
        valid={'action':'submit','result':{'ok':True}}
        for failed,code in (('{broken','syntax.invalid_json'),(json.dumps({'action':'submit','result':{'ok':False}}),'schema.const')):
            with self.subTest(code=code), TemporaryDirectory() as td:
                s=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(s.close)
                with endpoint([responses_raw(failed),responses_reply(valid)]) as (url,requests):
                    pid='clinical_letter.lmstudio.default'
                    s.configure_profile(pid,{'base_url':url,'model':'fixture','settings':{'reasoning':'high'}})
                    result=s.verify_provider(pid,'clinical_letter')
                    self.assertTrue(result['success'],result)
                    self.assertEqual(len(result['results']),1)
                    self.assertEqual(len(requests),2)
                    self.assertEqual((result['results'][0]['attempts'],result['results'][0]['repairs_used']),(2,1))
                    self.assertTrue(all(row['path'].endswith('/responses') for row in requests))
                    self.assertTrue(all('response_format' not in row['body'] for row in requests))
                    first=requests[0]['body']['input']; second=requests[1]['body']['input']
                    self.assertEqual(second[:2],first)
                    self.assertEqual(second[2],{'role':'assistant','content':failed})
                    self.assertEqual(second[3]['role'],'user')
                    self.assertIn(code,second[3]['content'])
                    envelope=json.loads(second[3]['content'][second[3]['content'].rfind('\n{')+1:])
                    self.assertEqual(envelope['failed_raw_response'],failed)
                    self.assertEqual(envelope['findings'][0]['code'],code)

    def test_lmstudio_verification_exhausts_configured_output_repairs(self):
        failed='{broken'
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(s.close)
            limit=s.execution_defaults['output_repair_retries']
            with endpoint([responses_raw(failed) for _ in range(limit+1)]) as (url,requests):
                pid='clinical_letter.lmstudio.default'
                s.configure_profile(pid,{'base_url':url,'model':'fixture','settings':{'reasoning':'high'}})
                result=s.verify_provider(pid,'clinical_letter')
                self.assertFalse(result['success'])
                self.assertEqual(len(result['results']),1)
                self.assertEqual(len(requests),limit+1)
                self.assertEqual((result['results'][0]['attempts'],result['results'][0]['repairs_used']),(limit+1,limit))
                self.assertEqual(result['results'][0]['error']['code'],'syntax_invalid')
                self.assertEqual(result['results'][0]['error']['findings'][0]['code'],'syntax.invalid_json')

    def test_lmstudio_verification_does_not_repair_provider_rejection(self):
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(s.close)
            with endpoint([responses_raw('{broken'),(400,{'error':'rejected'})]) as (url,requests):
                pid='clinical_letter.lmstudio.default'
                s.configure_profile(pid,{'base_url':url,'model':'fixture','settings':{'reasoning':'high'}})
                result=s.verify_provider(pid,'clinical_letter')
                self.assertFalse(result['success'])
                self.assertEqual(len(requests),2)
                self.assertEqual((result['results'][0]['attempts'],result['results'][0]['repairs_used']),(2,1))
                self.assertEqual(result['results'][0]['error']['code'],'provider_request_rejected')

    def test_http_pipeline_does_not_persist_secret(self):
        with TemporaryDirectory() as td:
            s=Service(APPS,td,PROFILES,GUIDES,FIXTURES);self.addCleanup(s.close)
            fixture=s.example('clinical_letter','standard')
            def resolve(value,payload):
                if isinstance(value,dict) and set(value)=={'$input'}:
                    obj=payload
                    for part in value['$input'].split('.'):
                        obj=obj[int(part)] if isinstance(obj,list) else obj[part]
                    return obj
                if isinstance(value,dict): return {k:resolve(v,payload) for k,v in value.items()}
                if isinstance(value,list): return [resolve(v,payload) for v in value]
                return value
            rows=[]
            for node in ['facts','facts_check','draft','draft_check']:
                template=fixture['responses'][node][0]
                rows.append(lambda request,template=template: responses_reply(resolve(template,json.loads(request['input'][-1]['content']))))
            with endpoint(rows) as (url,_):
                s.configure_profile('clinical_letter.lmstudio.default',{'base_url':url,'model':'fixture'},credential='sentinel-secret')
                rid=s.start('clinical_letter','clinical_letter.lmstudio.default','advanced',fixture['input'],developer=True);run=s.runner.advance(rid)
                self.assertEqual(run['status'],'waiting_review');self.assertNotIn('sentinel-secret',json.dumps(s.inspect(rid,developer=True)))

class CLIRegressionTests(unittest.TestCase):
    def test_cli_recorded_workflow_and_profile_listing(self):
        with TemporaryDirectory() as td:
            base=[sys.executable,'-m','localmedbot.cli','--apps',str(APPS),'--profiles',str(PROFILES),'--guidelines',str(GUIDES),'--fixtures',str(FIXTURES),'--data',td]
            rows=subprocess.run(base+['profiles','list','--workflow','clinical_letter'],capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(ROOT/'src')});self.assertEqual(rows.returncode,0,rows.stderr);self.assertEqual(len(json.loads(rows.stdout)),4)
            run=subprocess.run(base+['run','clinical_letter','--profile','clinical_letter.recorded.default','--example','standard'],capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(ROOT/'src')});self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(json.loads(run.stdout)['status'],'waiting_review')

HAS_FLASK=importlib.util.find_spec('flask') is not None
@unittest.skipUnless(HAS_FLASK,'Flask unavailable in this verification environment')
class WebBoundaryRegressionTests(unittest.TestCase):
    def setUp(self):
        from localmedbot.web import create_app
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES);self.addCleanup(self.s.close);self.app=create_app(self.s);self.addCleanup(self.app.extensions['localmedbot_pool'].shutdown);self.client=self.app.test_client();self.token=self.client.get('/api/session').get_json()['csrf']
    def post(self,path,data,headers=None):return self.client.post(path,json=data,headers=headers or {'X-LocalMedBot-Token':self.token})
    def test_host_origin_csrf_and_developer_boundary(self):
        self.assertEqual(self.client.post('/api/developer-mode',json={'enabled':True}).status_code,400)
        self.assertEqual(self.post('/api/developer-mode',{'enabled':True},headers={'X-LocalMedBot-Token':self.token,'Origin':'https://outside.invalid'}).status_code,400)
        self.assertEqual(self.client.get('/',headers={'Host':'outside.invalid'}).status_code,400)
        self.assertEqual(self.client.get('/api/dev/workflows/clinical_letter/steps').status_code,403)

if __name__=='__main__':unittest.main()
