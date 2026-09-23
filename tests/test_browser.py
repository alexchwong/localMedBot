"""Real Chromium user-action regressions. Opt in with LOCALMEDBOT_BROWSER_TESTS=1."""
from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util,os,shutil,unittest
from unittest.mock import patch

HAS_FLASK=importlib.util.find_spec('flask') is not None
HAS_PLAYWRIGHT=importlib.util.find_spec('playwright') is not None
ROOT=Path(__file__).resolve().parents[1];APPS=ROOT/'applications';PROFILES=ROOT/'model_profiles';GUIDES=ROOT/'guideline_sets';FIXTURES=ROOT/'tests/fixtures/steps'

@unittest.skipUnless(HAS_FLASK and HAS_PLAYWRIGHT and os.environ.get('LOCALMEDBOT_BROWSER_TESTS')=='1','Flask/Playwright browser verification not enabled')
class BrowserRegressionTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        from localmedbot.service import Service
        from localmedbot.web import create_app
        from tests.browser_harness import mount_browser_ui
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES);self.addCleanup(self.s.close);app=create_app(self.s);self.app=app;self.addCleanup(app.extensions['localmedbot_pool'].shutdown);self.p=sync_playwright().start();self.addCleanup(self.p.stop);browser_path=shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome');launch={'headless':True};launch.update({'executable_path':browser_path} if browser_path else {});self.browser=self.p.chromium.launch(**launch);self.addCleanup(self.browser.close);self.page=self.browser.new_page(viewport={'width':1366,'height':768});self.client=mount_browser_ui(self.page,app,ROOT)
    def wait(self,state):self.page.wait_for_function('(s)=>document.querySelector("#status").dataset.state===s',arg=state,timeout=20000)
    def test_recorded_letter_review_and_source(self):
        p=self.page
        # Required 1366x768 clinical controls are visible without page scrolling.
        for selector in ('#notes','#purpose','#start'):
            box=p.locator(selector).bounding_box(); self.assertIsNotNone(box); self.assertGreaterEqual(box['y'],0); self.assertLessEqual(box['y']+box['height'],768)
        p.locator('#settings-disclosure').click();p.locator('#model').fill('browser-edit');self.assertEqual(p.locator('#model').input_value(),'browser-edit');p.locator('#settings-disclosure').click()
        p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');self.assertEqual(p.get_by_test_id('citation').count(),0);self.assertNotIn('[1]',p.get_by_test_id('output').text_content());p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('completed')
    def test_polling_preserves_review_edits(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review')
        p.locator('#actor').fill('Unsaved local reviewer');p.locator('#comments').fill('Unsaved local comment')
        p.locator('#facts-panel details summary').click();omission=p.locator('[data-fact]').first;reason=p.locator('[data-reason]').first;omission.check();reason.fill('Unsaved omission reason')
        p.wait_for_timeout(1300)
        self.assertEqual(p.locator('#actor').input_value(),'Unsaved local reviewer');self.assertEqual(p.locator('#comments').input_value(),'Unsaved local comment');self.assertTrue(omission.is_checked());self.assertEqual(reason.input_value(),'Unsaved omission reason')
    def test_late_poll_and_error_cannot_replace_new_run_selection(self):
        p=self.page
        p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');clinical=self.s.store.runs()[0]['id']
        p.locator('#workflow').select_option('guideline_qa');p.locator('#profile').select_option('guideline_qa.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');guideline=next(r['id'] for r in self.s.store.runs() if r['id']!=clinical)
        p.evaluate("""(rid)=>{const real=window.fetch;let delayed=false;window.fetch=async(path,opts)=>{if(!delayed&&String(path).endsWith('/api/runs/'+rid)){delayed=true;await new Promise(r=>setTimeout(r,700));throw new Error('stale poll failure');}return real(path,opts)}}""",clinical)
        recent=p.locator('summary').filter(has_text='Recent runs')
        if not p.locator('#runs').is_visible(): recent.click()
        p.locator('#runs .run-row').filter(has_text='Clinical Letter').click();p.locator('#runs .run-row').filter(has_text='Guideline QA').click();p.wait_for_timeout(1100)
        self.assertIn(guideline,p.locator('#provenance').text_content());self.assertNotIn(clinical,p.locator('#provenance').text_content());self.assertTrue(p.locator('#error').is_hidden())
    def test_guideline_conflict_requires_explicit_ack(self):
        p=self.page;p.locator('#workflow').select_option('guideline_qa');p.locator('#profile').select_option('guideline_qa.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('conflict');p.get_by_test_id('start').click();self.wait('waiting_review');p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('waiting_review');p.locator('[data-conflict]').first.check();p.locator('#approve').click();self.wait('completed')
    def test_developer_mode_exposes_step_tester(self):
        p=self.page;p.locator('#workspace-selector').select_option('developer');p.locator('#dev-tab-step').click();p.locator('#new-fixture').wait_for(state='visible');self.assertFalse(p.locator('#advanced-option').evaluate('(el)=>el.hidden'));p.locator('#step-help').focus();self.assertTrue(p.locator('#step-help').evaluate('(el)=>document.activeElement===el'))
    def test_workspace_purpose_and_frozen_rhs(self):
        p=self.page
        self.assertEqual(p.locator('#workspace-selector').input_value(),'clinical')
        self.assertEqual(p.locator('#purpose').input_value(),self.s.load('clinical_letter')['purposes']['default'])
        p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.get_by_test_id('start').click();self.wait('waiting_review')
        p.locator('#rhs-input-tab').click();self.assertTrue(p.locator('#run-input').is_visible());self.assertFalse(p.get_by_test_id('output').is_visible())
        p.locator('#workspace-selector').select_option('developer')
        p.wait_for_function("()=>document.querySelector('#developer-workspace').offsetParent!==null")
        self.assertTrue(self.client.get('/api/session').get_json()['developer_enabled']);self.assertTrue(p.locator('#dev-steps').is_visible())
        p.locator('#workspace-selector').select_option('clinical');self.assertFalse(self.client.get('/api/session').get_json()['developer_enabled'])
    def test_developer_full_run_and_structured_inspection(self):
        p=self.page;p.locator('#workspace-selector').select_option('developer');p.locator('#dev-tab-full').click()
        p.locator('#dev-profile').select_option('clinical_letter.recorded.default');p.locator('#dev-input-mode').select_option('demo');p.locator('#dev-generate').click();self.wait('waiting_review')
        self.assertTrue(p.locator('#developer-workspace').is_visible());self.assertTrue(p.locator('#dev-progress progress').is_visible())
        p.locator('#dev-input-tab').click();self.assertTrue(p.locator('#dev-run-input').is_visible());self.assertFalse(p.locator('#dev-output').is_visible())
        p.locator('#dev-output-tab').click();self.assertTrue(p.locator('#dev-output').is_visible())
        p.locator('#view-json').click();self.assertTrue(p.locator('#audit').text_content().lstrip().startswith('{'))
        p.locator('#view-yaml').click();self.assertTrue(p.locator('#audit').text_content().lstrip().startswith('"'))
        p.locator('#dev-actor').fill('Synthetic developer reviewer');p.locator('#dev-approve').click();self.wait('completed')
    def test_workspace_failed_transition_reconciles_with_session(self):
        p=self.page;p.evaluate("""()=>{const original=window.fetch;window.fetch=(url,...rest)=>String(url)==='/api/developer-mode'?Promise.reject(new Error('synthetic network failure')):original(url,...rest)}""")
        p.locator('#workspace-selector').select_option('developer')
        p.wait_for_function("()=>!document.querySelector('#workspace-selector').disabled")
        self.assertEqual(p.locator('#workspace-selector').input_value(),'clinical')
        self.assertFalse(self.client.get('/api/session').get_json()['developer_enabled'])
    def test_workspace_bootstrap_uses_existing_backend_session(self):
        from tests.browser_harness import mount_browser_ui
        p=self.page;p.locator('#workspace-selector').select_option('developer')
        self.assertTrue(self.client.get('/api/session').get_json()['developer_enabled'])
        second=self.browser.new_page(viewport={'width':1366,'height':768});self.addCleanup(second.close)
        mount_browser_ui(second,self.app,ROOT,client=self.client)
        self.assertEqual(second.locator('#workspace-selector').input_value(),'developer')
        self.assertTrue(second.locator('#developer-workspace').is_visible())
        self.assertFalse(second.locator('#clinical-workspace').is_visible())
    def test_provider_verification_failure_is_readable(self):
        p=self.page;p.locator('#settings-disclosure').click()
        result={'success':False,'profile_id':'test-profile','results':[{'success':False,'role':'writing','error':{'code':'provider_request_rejected','explanation':'Synthetic request rejected','remedy':'Check synthetic settings','findings':[]}}]}
        with patch.object(self.s,'verify_provider',return_value=result):
            p.locator('#verify').click()
            p.wait_for_function("()=>document.querySelector('#verify-result').textContent.includes('Synthetic request rejected')")
            text=p.locator('#verify-result').text_content();self.assertIn('Check synthetic settings',text);self.assertNotIn('{\n  "success"',text)
    def test_reasoning_setting_is_editable_for_http_profiles(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.lmstudio.default');p.locator('#settings-disclosure').click()
        self.assertEqual(p.locator('#reasoning option').evaluate_all('(rows)=>rows.map(x=>x.value)'),['default','none','low','medium','high'])
        p.locator('#model').fill('fixture-model');p.locator('#reasoning').select_option('high');p.locator('#save-profile').click();p.wait_for_function("()=>document.querySelector('#verify-result').textContent.length>0")
        resolved=self.s.profiles.resolve('clinical_letter.lmstudio.default',{},workflow_id='clinical_letter',runnable=False)
        self.assertEqual(resolved['settings']['reasoning'],'high')

if __name__=='__main__':unittest.main()
