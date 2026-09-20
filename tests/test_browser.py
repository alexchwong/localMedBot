"""Real Chromium actions. Set LOCALMEDBOT_BROWSER_TESTS=1 to enable."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import os
import unittest
from werkzeug.serving import make_server
from localmedbot.service import Service
from localmedbot.web import create_app

APPS=Path(__file__).resolve().parents[1]/'applications'


@unittest.skipUnless(os.environ.get('LOCALMEDBOT_BROWSER_TESTS')=='1','Run explicitly with Chromium installed')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=Service(APPS,self.tmp.name)
        app=create_app(self.service)
        self.addCleanup(app.extensions['localmedbot_pool'].shutdown)
        self.server=make_server('127.0.0.1',0,app,threaded=True)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.p=sync_playwright().start();self.addCleanup(self.p.stop)
        options={'headless':True}
        if os.environ.get('LOCALMEDBOT_BROWSER_EXECUTABLE'):
            options.update(executable_path=os.environ['LOCALMEDBOT_BROWSER_EXECUTABLE'],args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
        self.browser=self.p.chromium.launch(**options);self.addCleanup(self.browser.close)
        self.page=self.browser.new_page(viewport={'width':1440,'height':1050})
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')
        self.page.wait_for_function("() => document.querySelector('#input').value.length > 0")

    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()

    def wait_state(self,state):
        self.page.wait_for_function('(state)=>document.querySelector("#status").dataset.state===state',arg=state,timeout=20000)

    def test_letter_review_revision_and_source_inspection(self):
        p=self.page;p.get_by_test_id('start').click();self.wait_state('waiting_review')
        p.get_by_test_id('citation').first.click()
        p.get_by_test_id('source-content').wait_for(state='visible')
        self.assertTrue(p.get_by_test_id('source-content').is_visible())
        source=json.loads(p.get_by_test_id('source-content').text_content())
        self.assertIn('passage',source['origin'])
        old=int(p.get_by_test_id('output').get_attribute('data-revision'))
        rid=p.locator('#run-id').text_content()
        p.get_by_test_id('actor').fill('Browser fixture reviewer')
        p.get_by_test_id('approve').click();self.wait_state('completed')
        p.get_by_test_id('comments').fill('Clarify purpose');p.get_by_test_id('revise').click();self.wait_state('pending')
        p.get_by_test_id('resume').click();self.wait_state('waiting_review')
        self.assertGreater(int(p.get_by_test_id('output').get_attribute('data-revision')),old)
        status=p.evaluate('''async ({rid,revision})=>{const r=await fetch('/api/runs/'+rid+'/review',{method:'POST',headers:{'Content-Type':'application/json','X-LocalMedBot-Token':document.querySelector('meta[name=request-token]').content},body:JSON.stringify({revision,actor:'fixture',decision:'approve'})});return r.status}''',{'rid':rid,'revision':old})
        self.assertEqual(status,409)
        p.get_by_test_id('approve').click();self.wait_state('completed')
        if os.environ.get('LOCALMEDBOT_SCREENSHOT_DIR'):
            dest=Path(os.environ['LOCALMEDBOT_SCREENSHOT_DIR']);dest.mkdir(exist_ok=True,parents=True)
            p.screenshot(path=str(dest/'letter-desktop.png'),full_page=True)
            p.set_viewport_size({'width':390,'height':844});p.get_by_test_id('output').scroll_into_view_if_needed()
            p.screenshot(path=str(dest/'letter-mobile.png'),full_page=False)
            self.assertLessEqual(p.evaluate('document.documentElement.scrollWidth'),390)

    def test_guideline_import_and_unresolved_answer(self):
        p=self.page;p.get_by_test_id('application').select_option('guideline_qa')
        p.wait_for_function("() => document.querySelector('#input').value.includes('Lumora')")
        p.get_by_test_id('ingest').click()
        p.wait_for_function("() => document.querySelector('#import-result').textContent.length > 0")
        p.get_by_test_id('example').select_option('conflict')
        p.wait_for_function("() => document.querySelector('#input').value.includes('Zelora')")
        p.get_by_test_id('start').click();self.wait_state('waiting_review')
        rid=p.locator('#run-id').text_content()
        out=self.service.store.artifact(rid,'output')['payload']
        self.assertEqual(out['claim_ids'],[]);self.assertEqual(len(out['unresolved']),1)
        p.get_by_test_id('actor').fill('Fixture reviewer');p.get_by_test_id('approve').click();self.wait_state('completed')

    def test_source_text_cannot_execute_script(self):
        rid=self.service.start('clinical_letter')
        run=self.service.store.run(rid)
        payload='<img src=x onerror="window.fixtureExecuted=true">'
        run['snapshot']['recording']['draft'][0]['claims'][0]['text']=payload
        self.service.store.put_run(run);self.service.runner.advance(rid)
        p=self.page;p.reload();p.locator('.run').first.click();self.wait_state('waiting_review')
        self.assertEqual(p.get_by_test_id('output').locator('img').count(),0)
        self.assertIsNone(p.evaluate('window.fixtureExecuted'))
