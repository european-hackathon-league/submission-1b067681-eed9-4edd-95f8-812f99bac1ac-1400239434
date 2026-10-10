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

    async def test_async_reset_blocks_continue(self):
        await self.page.set_content('<input aria-label="Quantity" oninput="setTimeout(()=>this.value=\'reset\',0)">'
                                    '<button onclick="document.body.dataset.advanced=1">Continue</button>')
        report = await fill_fields(self.page.evaluate, [('Quantity','42')], then_click='Continue')
        self.assertIn('FAILED readback', report)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-advanced'))

    async def test_ambiguous_visible_labels_require_model_choice(self):
        await self.page.set_content('<input aria-label="Amount"><input aria-label="Amount">')
        result = await self.page.evaluate(SET_JS, ['Amount','42'])
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(await self.page.locator('input').first.input_value(), '')

    async def test_output_caption_is_not_a_toggle(self):
        await self.page.set_content('<span role="group" onclick="window.clicked=true">'
                                    '<output aria-label="Mode: manually confirmed">Manually confirmed</output></span>')
        result = await self.page.evaluate(SET_JS, ['Manually confirmed','true'])
        self.assertEqual(result['status'], 'read-only')
        self.assertIsNone(await self.page.evaluate('window.clicked'))
        self.assertIn('read-only output', await self.page.evaluate(EXTRACT_JS, {}))

    async def test_duplicate_gates_are_filled_only_within_observed_scope(self):
        from swarm.forms import scoped_evaluator, SLIDER_JS
        gate = '<section><input type="range" aria-label="Unlock" value="0"><input type="checkbox" aria-label="Confirm"></section>'
        await self.page.set_content(gate + gate)
        await self.page.evaluate('window.__swarmGuard = {refs: [...document.querySelectorAll("section")]}')
        evaluate = scoped_evaluator(self.page.evaluate, 1)
        spot = await evaluate(SLIDER_JS, 'Unlock')
        self.assertIsNotNone(spot)
        report = await fill_fields(evaluate, [('Unlock','max'), ('Confirm','true')])
        self.assertNotIn('FAILED', report)
        self.assertEqual(await self.page.locator('[type=range]').nth(0).input_value(), '0')
        self.assertFalse(await self.page.locator('[type=checkbox]').nth(0).is_checked())
        self.assertEqual(await self.page.locator('[type=range]').nth(1).input_value(), '100')
        self.assertTrue(await self.page.locator('[type=checkbox]').nth(1).is_checked())
        await self.page.locator('section').nth(1).evaluate('(el) => el.remove()')
        with self.assertRaises(Exception):
            await evaluate(SET_JS, ['Unlock','max'])


class InterruptedCollection(unittest.IsolatedAsyncioTestCase):
    async def test_browsing_steps_reuse_one_client(self):
        from swarm.browser_agent import QwenChat, ChatOpenAI
        async def create(**kwargs): return NS(choices=[], usage=None)
        client = NS(chat=NS(completions=NS(create=create)))
        llm = QwenChat(model='fixture')
        with patch.object(ChatOpenAI, 'get_client', return_value=client) as factory:
            for _ in range(3):
                await llm.get_client().chat.completions.create(messages=[])
            self.assertEqual(factory.call_count, 1)

    async def test_positions_publish_while_qa_is_still_pending(self):
        from agents import analyze_quantities
        ctx = Context(task={'brief':'','access':[]}, board=Blackboard(), llm=None, pool=None)
        await ctx.board.post(Evidence('tender.page','t',{'url':'fixture','text':'a bill of quantities'}))
        async def fake_extract(ctx, instruction, shape, blocks, **kwargs):
            if kwargs['list_key'] == 'positions':
                return {'positions':[{'position':'1','quantity':'2','unit':'Stk'}]}
            await asyncio.Event().wait()
        with patch.object(analyze_quantities, 'extract', fake_extract):
            task = asyncio.create_task(analyze_quantities.refresh(ctx))
            result = await ctx.board.wait_for(key='quantities', timeout=2)
            self.assertEqual(result['1']['quantity'], 2)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(ctx.shared['tender.extracted']), 1)

    async def test_spec_table_publishes_before_slow_document(self):
        from agents import analyze_requirements
        ctx = Context(task={'brief':'','access':[]}, board=Blackboard(), llm=None, pool=None)
        for text in ('quick', 'slow'):
            await ctx.board.post(Evidence('spec.page','s',{'url':text,'text':text}))
        async def fake_extract(ctx, instruction, shape, blocks, **kwargs):
            if 'quick' in blocks[0]:
                return {'revisions':[{'revision':'C','is_current':True}],
                        'requirements':[{'ref':'X','revision':'C','bore':'73'}]}
            await asyncio.Event().wait()
        with patch.object(analyze_requirements, 'extract', fake_extract):
            task = asyncio.create_task(analyze_requirements.analyse(ctx))
            result = await ctx.board.wait_for(key='requirements', timeout=2)
            self.assertEqual(result['X']['bore'], '73')
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_contradicted_complete_site_mapping_is_replaced(self):
        from swarm.sites import resolve_roles
        class LLM:
            async def ask_json(self, *args, **kwargs): return {'erp':'second'}
        ctx = Context(task={'brief':'','access':[{'siteId':'first'},{'siteId':'second'}]},
                      board=Blackboard(), llm=LLM(), pool=None)
        ctx.board.results['sites'] = dict.fromkeys(('tender','spec','erp','mail'), 'first')
        ctx.shared['site_invalidations'] = {'erp':{'first'}}
        self.assertEqual((await resolve_roles(ctx))['erp'], 'second')

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
            self.assertIn('Discount percent: 0,00.', goal)
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
    def test_invalid_optional_fields_do_not_discard_independent_credit(self):
        from swarm.validation import prepare_answer
        answer = {'decision':'bid', 'positions':[
            {'position':'1','material_number':'M','quantity':2,'unit_price':10,'lead_time_weeks':3,'rebate_total':-1},
            {'position':'2','material_number':'M','quantity':1,'unit_price':10,'lead_time_weeks':None}], 'total_net':30}
        clean, issues = prepare_answer(answer)
        self.assertEqual(len(clean['positions']), 1)
        self.assertNotIn('rebate_total', clean['positions'][0])
        self.assertEqual(clean['total_net'], 20)
        self.assertEqual(len(issues), 2)
        self.assertEqual(len(answer['positions']), 2)

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
