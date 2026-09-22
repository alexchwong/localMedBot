"""Browser/API contract tests. They skip only when the declared Flask dependency is unavailable."""
from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util,json,time,unittest,uuid

HAS_FLASK=importlib.util.find_spec('flask') is not None
ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT/'applications'; PROFILES=ROOT/'model_profiles'; GUIDES=ROOT/'guideline_sets'; FIXTURES=ROOT/'tests/fixtures/steps'

@unittest.skipUnless(HAS_FLASK,'Flask is not installed in this verification environment')
class WebApiTests(unittest.TestCase):
    def setUp(self):
        from localmedbot.service import Service
        from localmedbot.web import create_app
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES); self.addCleanup(self.service.close)
        self.app=create_app(self.service); self.addCleanup(self.app.extensions['localmedbot_pool'].shutdown)
        self.client=self.app.test_client(); state=self.client.get('/api/session').get_json(); self.token=state['csrf']
    def post(self,path,data=None,client=None,token=None):
        c=client or self.client; return c.post(path,json={} if data is None else data,headers={'X-LocalMedBot-Token':token or self.token})
    def test_developer_mode_is_session_scoped(self):
        self.assertEqual(self.client.get('/api/dev/workflows/clinical_letter/steps').status_code,403)
        self.assertFalse(self.client.get('/api/session').get_json()['developer_enabled'])
        self.assertTrue(self.post('/api/developer-mode',{'enabled':True}).get_json()['developer_enabled'])
        self.assertEqual(self.client.get('/api/dev/workflows/clinical_letter/steps').status_code,200)
        other=self.app.test_client(); osess=other.get('/api/session').get_json(); self.assertFalse(osess['developer_enabled'])
        self.assertEqual(other.get('/api/dev/workflows/clinical_letter/steps').status_code,403)
        self.assertEqual(self.client.post('/api/developer-mode',json={'enabled':False},headers={'X-LocalMedBot-Token':'wrong'}).status_code,400)
    def test_profile_secret_never_returned(self):
        secret='browser-fixture-secret'; pid='clinical_letter.openrouter.default'
        r=self.post('/api/model-profiles/configure',{'profile_id':pid,'overlay':{'model':'fixture-model'},'credential':secret}); self.assertEqual(r.status_code,200); self.assertTrue(r.get_json()['credential_present']); self.assertNotIn(secret,r.get_data(as_text=True))
        self.assertNotIn(secret,json.dumps(self.client.get('/api/model-profiles?workflow_id=clinical_letter').get_json()))
    def test_developer_self_run_and_handoff(self):
        self.post('/api/developer-mode',{'enabled':True})
        d={'workflow_id':'clinical_letter','profile_id':'clinical_letter.self.default','input_mode':'free_text','input':{'notes':'Synthetic note.','purpose':'Update'}}
        r=self.post('/api/runs',d); self.assertEqual(r.status_code,202); rid=r.get_json()['id']
        for _ in range(100):
            state=self.client.get('/api/runs/'+rid).get_json()
            if state['run']['status'] not in {'pending','running'}: break
            time.sleep(.01)
        else: self.fail('run did not leave pending/running within the polling budget')
        self.assertEqual(state['run']['status'],'waiting_model'); self.assertEqual(state['handoff']['node_id'],'facts')
    def test_legacy_copy_requires_fresh_start_and_preserves_input(self):
        fixture=self.service.example('clinical_letter','standard')['input']; self.service.store.put_run({'id':'legacy','status':'completed','input':fixture,'snapshot':{'manifest':{'id':'clinical_letter'}},'origins':{'task_input':'synthetic','evidence':{},'revision_feedback':{}}})
        r=self.post('/api/runs/legacy/copy-input',{}); self.assertEqual(r.status_code,200); copied=r.get_json(); self.assertEqual(copied['input'],fixture)
        # Arbitrary replacement input is ignored; server consumes the session-bound copy token.
        self.post('/api/developer-mode',{'enabled':True})
        start=self.post('/api/runs',{'workflow_id':'guideline_qa','profile_id':'clinical_letter.self.default','input_mode':'copied','copy_input_id':copied['copy_input_id'],'input':{'replacement':'not allowed'}})
        self.assertEqual(start.status_code,202)
        rid=start.get_json()['id']; run=self.service.store.run(rid); self.assertEqual(run['input'],fixture); self.assertEqual(run['derived_from_run_id'],'legacy')
        again=self.post('/api/runs',{'workflow_id':'clinical_letter','profile_id':'clinical_letter.self.default','input_mode':'copied','copy_input_id':copied['copy_input_id']})
        self.assertEqual(again.status_code,400)

HAS_PLAYWRIGHT=importlib.util.find_spec('playwright') is not None
@unittest.skipUnless(HAS_FLASK and HAS_PLAYWRIGHT,'Flask/Playwright browser runtime unavailable')
@unittest.skipUnless(__import__('os').environ.get('LOCALMEDBOT_BROWSER_TESTS')=='1','Real browser tests are opt-in')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        import shutil
        from playwright.sync_api import sync_playwright
        from localmedbot.service import Service
        from localmedbot.web import create_app
        from tests.browser_harness import mount_browser_ui
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.service=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES); self.addCleanup(self.service.close)
        self.app=create_app(self.service); self.addCleanup(self.app.extensions['localmedbot_pool'].shutdown)
        self.p=sync_playwright().start(); self.addCleanup(self.p.stop); browser_path=shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome'); launch={'headless':True}; launch.update({'executable_path':browser_path} if browser_path else {}); self.browser=self.p.chromium.launch(**launch); self.addCleanup(self.browser.close); self.page=self.browser.new_page(viewport={'width':1366,'height':768}); self.client=mount_browser_ui(self.page,self.app,ROOT)
    def wait(self,state): self.page.wait_for_function('(s)=>document.querySelector("#status").dataset.state===s',arg=state,timeout=20000)
    def test_conflict_requires_explicit_acknowledgement(self):
        p=self.page; p.get_by_test_id('application').select_option('guideline_qa'); p.locator('#profile').select_option('guideline_qa.recorded.default'); p.locator('#input-mode').select_option('demo'); p.locator('#example').select_option('conflict'); p.get_by_test_id('start').click(); self.wait('waiting_review'); p.locator('#actor').fill('Browser reviewer'); p.locator('#approve').click(); self.wait('waiting_review'); p.locator('[data-conflict]').first.check(); p.locator('#approve').click(); self.wait('completed')
