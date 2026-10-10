"""Regression checks for unfamiliar UI structures and interrupted collection."""
import asyncio
import io
import unittest
import zipfile
from types import SimpleNamespace as NS
from unittest.mock import patch

from playwright.async_api import async_playwright
from swarm.blackboard import Blackboard, Evidence
from swarm.context import Context
from swarm.forms import SET_JS, fill_fields
from swarm.page_text import EXTRACT_JS, NEXT_JS


class BrowserControls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_shadow_evidence_and_css_decoys(self):
        await self.page.set_content('<div style="display:none">obsolete secret decoy</div><x-view></x-view>')
        await self.page.evaluate("document.querySelector('x-view').attachShadow({mode:'open'}).innerHTML='<p>Current revision Z</p>'")
        text = await self.page.evaluate(EXTRACT_JS, {})
        self.assertIn('Current revision Z', text)
        self.assertNotIn('obsolete', text)

    async def test_hidden_duplicate_does_not_take_visible_value(self):
        await self.page.set_content('<input name="amount" style="display:none"><label>Amount<input name="amount"></label>')
        self.assertEqual((await self.page.evaluate(SET_JS, ['amount', '42']))['status'], 'ok')
        self.assertEqual(await self.page.locator('input').nth(0).input_value(), '')
        self.assertEqual(await self.page.locator('input').nth(1).input_value(), '42')

    async def test_invalid_radio_does_not_advance(self):
        await self.page.set_content('<label><input type="radio" name="choice" value="one">First</label>'
                                    '<button onclick="document.body.dataset.advanced=1">Continue</button>')
        report = await fill_fields(self.page.evaluate, [('choice', 'absent')], then_click='Continue')
        self.assertIn('FAILED', report)
        self.assertFalse(await self.page.locator('input').is_checked())
        self.assertIsNone(await self.page.locator('body').get_attribute('data-advanced'))

    async def test_contenteditable_in_shadow_root(self):
        await self.page.set_content('<x-form></x-form>')
        await self.page.evaluate("document.querySelector('x-form').attachShadow({mode:'open'}).innerHTML='<div contenteditable=true aria-label=Memo></div>'")
        result = await self.page.evaluate(SET_JS, ['Memo', 'verified text'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(await self.page.locator('[contenteditable]').text_content(), 'verified text')


class InterruptedCollection(unittest.IsolatedAsyncioTestCase):
    async def test_signer_reuses_pending_tan_and_requires_receipt(self):
        from agents import submit_bid
        from swarm.browser_agent import build_tools
        ctx = Context(task={'brief': 'Submit the bid.', 'submission': {'kind':'site'}, 'access':[
            {'siteId':'t','url':'https://t.example'}, {'siteId':'m','url':'https://m.example'}]},
            board=Blackboard(), llm=None, pool=None)
        ctx.board.results.update({'sites': {'tender':'t','spec':'t','erp':'t','mail':'m'},
                                  'brief': {'tender_reference':'FIXTURE'}})
        release = asyncio.Event()
        mail_calls = 0
        portal_calls = 0
        class Session:
            async def get_current_page_url(self): return 'https://t.example/receipt'
        async def page_text(session): return 'Bid FIXTURE successfully submitted. Receipt 56789.'
        async def fake_browse(ctx, goal, **kwargs):
            nonlocal mail_calls, portal_calls
            if kwargs['browser'] == 3:
                mail_calls += 1
                await release.wait()
                await kwargs['on_record']({'kind':'mail.tan','data':{'tan':'753951'},'source':'mail','note':''})
                return {'summary':'TAN recorded', 'done':True, 'steps':1}
            portal_calls += 1
            if portal_calls == 1:
                # A model's done=True alone must never end the signing workflow.
                return {'summary':'Stopped without receipt', 'done':True, 'steps':1}
            tools = build_tools(ctx, 't', 'test', None)
            kwargs['extra_tools'](tools)
            actions = tools.registry.registry.actions
            tan = actions['get_signing_tan'].function
            pending = await tan(params=submit_bid.TanParams())
            self.assertIn('pending', pending.extracted_content)
            release.set()
            result = await tan(params=submit_bid.TanParams())
            self.assertEqual(result.extracted_content, 'TAN: 753951')
            confirm = actions['confirm_submission'].function
            bad = await confirm(params=submit_bid.ReceiptParams(quote='This text was invented',reference='FIXTURE'), browser_session=Session())
            self.assertTrue(bad.error)
            good = await confirm(params=submit_bid.ReceiptParams(quote='Bid FIXTURE successfully submitted.',reference='FIXTURE'), browser_session=Session())
            self.assertTrue(good.success)
            return {'summary':'Confirmed receipt', 'done':True, 'steps':2}
        with patch.object(submit_bid, 'browse', fake_browse), patch.object(submit_bid, 'read_current_page', page_text):
            await submit_bid.BidSubmitter(ctx).run()
        self.assertEqual((mail_calls, portal_calls), (1, 2))
        self.assertTrue(ctx.board.get('submission')['accepted'])

    async def test_completed_erp_extraction_survives_sibling_cancellation(self):
        from agents import analyze_materials
        ctx = Context(task={'brief': '', 'access': []}, board=Blackboard(), llm=None, pool=None)
        for name in ('quick', 'slow'):
            await ctx.board.post(Evidence('erp.page', 'erp', {'url': name, 'topic': 'erp search', 'text': name}))
        completed = asyncio.Event()
        async def fake_extract(ctx, instruction, shape, blocks, **kwargs):
            if 'quick' in blocks[0]:
                completed.set()
                return {'materials': [{'material': 'fixture'}]}
            await asyncio.Event().wait()
        with patch.object(analyze_materials, 'extract', fake_extract):
            task = asyncio.create_task(analyze_materials.extract_erp(ctx))
            await asyncio.wait_for(completed.wait(), 2)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(ctx.shared['erp.extracted']), 1)
        self.assertFalse(ctx.lock('erp.extract').locked())

    async def test_partial_role_mapping_is_retried_and_roles_may_share_site(self):
        from swarm.sites import resolve_roles
        class LLM:
            calls = 0
            async def ask_json(self, *args, **kwargs):
                self.calls += 1
                return {'tender': 'opaque'} if self.calls == 1 else dict.fromkeys(('tender','spec','erp','mail'), 'opaque')
        ctx = Context(task={'brief': 'All capabilities share a workspace.', 'access': [{'siteId':'opaque'}]},
                      board=Blackboard(), llm=LLM(), pool=None)
        self.assertEqual(len(await resolve_roles(ctx)), 4)
        self.assertEqual(ctx.llm.calls, 2)


class DocumentsAndValidation(unittest.TestCase):
    def test_zip_selection_requires_no_filesystem_extraction(self):
        from swarm.documents import document_text
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('selected.txt', 'Selected technical specification')
        self.assertIn('selected.txt', document_text(data.getvalue()))
        self.assertEqual(document_text(data.getvalue(), 'selected.txt'), 'Selected technical specification')

    def test_answer_arithmetic_and_runtime_schema(self):
        from swarm.validation import answer_issues
        answer = {'tender_reference':'fixture', 'decision':'bid', 'positions':[
            {'position':'1','quantity':2,'unit_price':1.25}], 'total_net':2.5}
        self.assertEqual(answer_issues(answer), [])
        self.assertTrue(answer_issues(dict(answer, total_net=3)))
        self.assertTrue(answer_issues(answer, {'required':['missing_field']}))


if __name__ == '__main__':
    unittest.main()
