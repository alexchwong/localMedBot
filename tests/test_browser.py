"""Real Chromium user-action regressions. Opt in with LOCALMEDBOT_BROWSER_TESTS=1."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import importlib.util,os,unittest

HAS_FLASK=importlib.util.find_spec('flask') is not None
HAS_PLAYWRIGHT=importlib.util.find_spec('playwright') is not None
ROOT=Path(__file__).resolve().parents[1];APPS=ROOT/'applications';PROFILES=ROOT/'model_profiles';GUIDES=ROOT/'guideline_sets';FIXTURES=ROOT/'tests/fixtures/steps'

@unittest.skipUnless(HAS_FLASK and HAS_PLAYWRIGHT and os.environ.get('LOCALMEDBOT_BROWSER_TESTS')=='1','Flask/Playwright browser verification not enabled')
class BrowserRegressionTests(unittest.TestCase):
    def setUp(self):
        from werkzeug.serving import make_server
        from playwright.sync_api import sync_playwright
        from localmedbot.service import Service
        from localmedbot.web import create_app
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Service(APPS,self.tmp.name,PROFILES,GUIDES,FIXTURES);self.addCleanup(self.s.close);app=create_app(self.s);self.addCleanup(app.extensions['localmedbot_pool'].shutdown);self.server=make_server('127.0.0.1',0,app,threaded=True);self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.stop);self.p=sync_playwright().start();self.addCleanup(self.p.stop);self.browser=self.p.chromium.launch(headless=True);self.addCleanup(self.browser.close);self.page=self.browser.new_page(viewport={'width':1200,'height':900});self.page.goto(f'http://127.0.0.1:{self.server.server_port}');self.page.wait_for_selector('#workflow')
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def wait(self,state):self.page.wait_for_function('(s)=>document.querySelector("#status").dataset.state===s',arg=state,timeout=20000)
    def test_recorded_letter_review_and_source(self):
        p=self.page;p.locator('#workflow').select_option('clinical_letter');p.locator('#profile').select_option('clinical_letter.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('standard');p.get_by_test_id('start').click();self.wait('waiting_review');p.get_by_test_id('citation').first.click();self.assertTrue(p.locator('#source-content').is_visible());p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('completed')
    def test_guideline_conflict_requires_explicit_ack(self):
        p=self.page;p.locator('#workflow').select_option('guideline_qa');p.locator('#profile').select_option('guideline_qa.recorded.default');p.locator('#input-mode').select_option('demo');p.locator('#example').select_option('conflict');p.get_by_test_id('start').click();self.wait('waiting_review');p.locator('#actor').fill('Browser fixture reviewer');p.locator('#approve').click();self.wait('waiting_review');p.locator('[data-conflict]').first.check();p.locator('#approve').click();self.wait('completed')
    def test_developer_mode_exposes_step_tester(self):
        p=self.page;p.locator('#developer').check();p.locator('#new-fixture').wait_for(state='visible');self.assertFalse(p.locator('#advanced-option').is_hidden())

if __name__=='__main__':unittest.main()
