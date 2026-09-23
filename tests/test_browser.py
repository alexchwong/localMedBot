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
        p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');self.assertEqual(p.get_by_test_id('citation').count(),0);self.assertNotIn('[1]',p.get_by_test_id('output').text_content());p.locator('#rhs-review-tab').click();p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('completed')
    def test_polling_preserves_review_edits(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review')
        p.locator('#rhs-review-tab').click();p.locator('#actor').fill('Unsaved local reviewer');p.locator('#comments').fill('Unsaved local comment')
        p.locator('#tab-details').click();omission=p.locator('[data-fact]').first;reason=p.locator('[data-reason]').first;omission.check();reason.fill('Unsaved omission reason')
        p.wait_for_timeout(1300)
        self.assertEqual(p.locator('#actor').input_value(),'Unsaved local reviewer');self.assertEqual(p.locator('#comments').input_value(),'Unsaved local comment');self.assertTrue(omission.is_checked());self.assertEqual(reason.input_value(),'Unsaved omission reason')
    def test_late_poll_and_error_cannot_replace_new_run_selection(self):
        p=self.page
        p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');clinical=self.s.store.runs()[0]['id']
        p.locator('#workflow').select_option('guideline_qa');p.locator('#profile').select_option('guideline_qa.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');guideline=next(r['id'] for r in self.s.store.runs() if r['id']!=clinical)
        p.evaluate("""(rid)=>{const real=window.fetch;let delayed=false;window.fetch=async(path,opts)=>{if(!delayed&&String(path).endsWith('/api/runs/'+rid)){delayed=true;await new Promise(r=>setTimeout(r,700));throw new Error('stale poll failure');}return real(path,opts)}}""",clinical)
        p.locator('#run-workflow-filter').select_option('clinical_letter');p.locator('#run-select').select_option(clinical)
        p.locator('#run-workflow-filter').select_option('guideline_qa');p.locator('#run-select').select_option(guideline);p.wait_for_timeout(1100)
        self.assertIn(guideline,p.locator('#provenance').text_content());self.assertNotIn(clinical,p.locator('#provenance').text_content());self.assertTrue(p.locator('#error').is_hidden())
    def test_independent_run_tracking_and_execution_history(self):
        p=self.page
        p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo')
        p.locator('#run-title-input').fill('Synthetic tracking label');p.get_by_test_id('start').click();self.wait('waiting_review')
        self.assertIn('Synthetic tracking label',p.locator('#result-title').text_content())
        self.assertGreater(p.locator('#model-stage option').count(),0)
        p.wait_for_function("()=>document.querySelector('#live-run').textContent.includes('Active run:')")
        p.locator('#workspace-selector').select_option('developer');p.locator('#dev-run-title').fill('Separate synthetic label')
        p.locator('#dev-profile').select_option('clinical_letter.recorded.default');p.locator('#dev-input-mode').select_option('demo');p.locator('#dev-generate').click();self.wait('waiting_review')
        p.wait_for_function("()=>document.querySelector('#result-title').textContent.includes('Separate synthetic label')")
        self.assertIn('Separate synthetic label',p.locator('#result-title').text_content())
        p.locator('#run-select option').first.wait_for(state='attached')
        p.locator('#workspace-selector').select_option('clinical');p.locator('#run-workflow-filter').select_option('clinical_letter')
        earlier=next(r['id'] for r in self.s.store.runs() if r.get('title')=='Synthetic tracking label')
        p.locator('#run-select').select_option(earlier)
        self.assertIn('Synthetic tracking label',p.locator('#result-title').text_content())
        p.wait_for_function("()=>document.querySelector('#live-run button')?.textContent.includes('Separate synthetic label')",timeout=5000)
        p.locator('#live-run button').click()
        p.wait_for_function("()=>document.querySelector('#result-title').textContent.includes('Separate synthetic label')",timeout=5000)
        self.assertIn('Separate synthetic label',p.locator('#result-title').text_content())
    def test_guideline_conflict_requires_explicit_ack(self):
        p=self.page;p.locator('#workflow').select_option('guideline_qa');p.locator('#profile').select_option('guideline_qa.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('conflict');p.get_by_test_id('start').click();self.wait('waiting_review');p.locator('#rhs-review-tab').click();p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('waiting_review');p.locator('[data-conflict]').first.check();p.locator('#approve').click();self.wait('completed')
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
    def test_multiline_user_input_and_model_call_tabs(self):
        p=self.page;notes='Synthetic first line\nSynthetic second line\\n literal sequence';p.locator('#workflow').select_option('clinical_letter')
        # The recorded path provides model calls without a remote endpoint.
        p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.get_by_test_id('start').click();self.wait('waiting_review')
        p.locator('#rhs-input-tab').click();self.assertIn('Purpose:',p.locator('#run-input').text_content())
        p.locator('#tab-input').click();self.assertGreater(p.locator('#model-call option').count(),0)
        p.locator('#model-stage').select_option(p.locator('#model-stage option').last.get_attribute('value'))
        self.assertGreater(p.locator('#model-call option').count(),0);self.assertIn('Actual input / messages sent',p.locator('#model-input').text_content());p.locator('#tab-output').click();self.assertTrue(p.locator('#model-output pre').count());self.assertEqual(p.locator('#model-output pre').text_content(),p.evaluate("()=>current.model_calls.find(c=>c.request_id===document.querySelector('#model-call').value).raw_response"));self.assertEqual(p.locator('#model-response-mode').count(),0)
        p.locator('#tab-retries').click();self.assertTrue(p.locator('#retry-history').is_visible());p.locator('#tab-details').click();self.assertTrue(p.locator('#facts-panel').is_visible())
        # Test-controlled run input exercises the presentation without a live model.
        p.evaluate("(notes)=>renderUserInput({input:{purpose:'synthetic',sources:[{content:notes}]},origins:{task_input:'user_supplied'},snapshot:{manifest:{id:'clinical_letter'},selected_purpose:{label:'Synthetic purpose'}}},document.querySelector('#run-input'))",notes)
        self.assertEqual(p.locator('#run-input .user-input-text').text_content(),notes)
        self.assertEqual(p.locator('#run-input .user-input-text').evaluate('(el)=>getComputedStyle(el).whiteSpace'),'pre-wrap')
        p.evaluate("(notes)=>renderMessages(document.querySelector('#model-input'),[{role:'user',content:notes}])",notes)
        self.assertEqual(p.locator('#model-input .message-text').last.text_content(),notes)
        self.assertEqual(p.locator('#model-input .message-text').last.evaluate('(el)=>getComputedStyle(el).whiteSpace'),'pre-wrap')
    def test_inspection_scroll_review_and_distinct_call_failures(self):
        p=self.page;self.assertTrue(p.locator('#rhs-review-tab').is_disabled());p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.get_by_test_id('start').click();self.wait('waiting_review')
        self.assertFalse(p.locator('#rhs-review-tab').is_disabled());p.locator('#rhs-review-tab').click();self.assertTrue(p.locator('.rhs #review').is_visible());self.assertIn(self.s.store.runs()[0]['id'],p.locator('#review-revision').text_content())
        self.assertTrue(p.locator('#review-output #run-output').is_visible());self.assertLess(p.locator('#review').bounding_box()['y'],p.locator('#review-output #run-output').bounding_box()['y'])
        p.locator('#rhs-output-tab').click();self.assertFalse(p.locator('#review').is_visible());self.assertTrue(p.locator('#run-output').is_visible());p.locator('#rhs-input-tab').click();self.assertTrue(p.locator('#run-input').is_visible());self.assertFalse(p.locator('#run-output').is_visible())
        self.assertEqual(p.locator('.run-panel').evaluate('(el)=>getComputedStyle(el).overflowY'),'hidden')
        for tab,pane in (('#tab-input','#model-input-panel'),('#tab-output','#model-output-panel')):
            p.locator(tab).click();self.assertEqual(p.locator(pane).evaluate('(el)=>getComputedStyle(el).overflowY'),'auto')
        self.assertEqual(p.locator('#model-input').evaluate('(el)=>getComputedStyle(el).overflowY'),'visible')
        self.assertEqual(p.locator('#model-output').evaluate('(el)=>getComputedStyle(el).overflowY'),'visible')
        first=dict(request_id='synthetic-a',node_id='synthetic',attempt=1,call_index=1,status='failed',messages=[dict(role='user',content='input '*500)],raw_response='not JSON',audit=dict(phase='syntax',findings=[dict(code='syntax.bad',problem='Invalid JSON')]))
        second={**first,'request_id':'synthetic-b','call_index':2,'raw_response':'{"document":"Readable document"}','parsed':{'document':'Readable document'},'audit':{'phase':'contract','findings':[{'code':'reference.bad','problem':'Passage absent'}]}}
        p.evaluate('(calls)=>{window.inspectionTestData={model_calls:calls,repairs:[],semantic_revisions:[],physical_calls:[]};renderExecution(inspectionTestData)}',[first,second])
        p.locator('#model-call').select_option('synthetic-a');p.evaluate('()=>renderCallDetail(inspectionTestData)');self.assertIn('not JSON',p.locator('#model-output').text_content());self.assertIn('syntax.bad',p.locator('#model-validation').text_content());self.assertNotIn('reference.bad',p.locator('#model-validation').text_content())
        p.locator('#model-call').select_option('synthetic-b');p.evaluate('()=>renderCallDetail(inspectionTestData)');self.assertEqual(p.locator('#model-output pre').text_content(),second['raw_response']);self.assertIn('reference.bad',p.locator('#model-validation').text_content());self.assertNotIn('syntax.bad',p.locator('#model-validation').text_content())
        p.evaluate("()=>{inspectionTestData.model_calls[1].raw_response=null;renderCallDetail(inspectionTestData)}");self.assertIn('No response was received.',p.locator('#model-output').text_content())
        p.locator('#tab-input').click();p.locator('#model-input-panel').evaluate('(el)=>el.scrollTop=el.scrollHeight');self.assertGreater(p.locator('#model-input-panel').evaluate('(el)=>el.scrollTop'),0)
        p.locator('#tab-output').click();self.assertTrue(p.locator('#model-input-panel').is_hidden());self.assertTrue(p.locator('#model-output-panel').is_visible())
    def test_repair_findings_and_feedback_share_one_section(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.get_by_test_id('start').click();self.wait('waiting_review')
        repair={'repair_id':'synthetic-repair','node_id':'draft','attempt':1,'repair_ordinal':1,'frozen_limit':2,'outcome':'corrected','audit_phase':'contract','failed_call_id':'synthetic-failed','next_request_id':'synthetic-next','findings':[{'code':'synthetic.reference','problem':'Unmatched passage'}],'rendered_feedback':'Correct the unmatched passage','feedback_issued':True}
        calls=[{'request_id':'synthetic-failed','raw_response':'failed response'},{'request_id':'synthetic-next','status':'completed','raw_response':'next response'}]
        p.evaluate('(d)=>{const data={repairs:[d.repair],semantic_revisions:[],model_calls:d.calls};document.querySelector("#retry-select").replaceChildren(new Option("Repair",`repair:${d.repair.repair_id}`));renderRetryDetail(data)}',{'repair':repair,'calls':calls})
        p.locator('#tab-retries').click();section=p.locator('#retry-detail .repair-correction');self.assertEqual(section.count(),1)
        self.assertIn('synthetic.reference',section.text_content());self.assertIn(repair['rendered_feedback'],section.text_content());self.assertIn('failed response',p.locator('#retry-detail').text_content());self.assertIn('next response',p.locator('#retry-detail').text_content())
    def test_omission_action_requests_draft_revision(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.get_by_test_id('start').click();self.wait('waiting_review')
        p.locator('#rhs-review-tab').click();p.locator('#actor').fill('Synthetic reviewer');p.locator('#tab-details').click();p.locator('[data-fact]').first.check()
        p.locator('#apply-omissions').click();p.wait_for_function("()=>!document.querySelector('#error').hidden");self.assertIn('reason',p.locator('#error').text_content().lower())
        p.locator('[data-reason]').first.fill('Synthetic reviewer decision');p.locator('#apply-omissions').click()
        p.wait_for_function("()=>['waiting_review','blocked','failed'].includes(document.querySelector('#status').dataset.state) && !document.querySelector('#apply-omissions').disabled",timeout=20000)
        run=self.s.store.runs()[0];self.assertNotEqual(run['status'],'pending');self.assertEqual(len(run['omission_policy']['omitted_fact_ids']),1)
        self.assertTrue(run['omission_policy']['reasons'])
        if run['status']=='waiting_review':
            p.locator('#approve').click();p.wait_for_function("()=>!document.querySelector('#error').hidden")
            self.assertIn('acknowledge',p.locator('#error').text_content().lower())
            p.locator('#omission-ack').check();p.locator('#approve').click();self.wait('completed')
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
