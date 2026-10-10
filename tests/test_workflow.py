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

    async def test_unlocked_inline_gates_do_not_reclassify_the_entire_mailbox(self):
        from swarm.guard import SCAN_JS
        await self.page.set_content('<section><label>Unlock<input type="range" value="100"></label>'
                                    '<label><input type="checkbox" checked>Confirm</label>'
                                    '<a href="/message">Read message</a></section>')
        scan = await self.page.evaluate(SCAN_JS)
        self.assertEqual(scan['items'], [])


class InterruptedCollection(unittest.IsolatedAsyncioTestCase):
    async def test_dialog_callback_does_not_block_cdp_response_receiver(self):
        from swarm.browser_agent import RobustSession, PopupsWatchdog
        from cdp_use.cdp.registry import EventRegistry
        registry = EventRegistry()
        sent, response = asyncio.Event(), asyncio.Event()
        calls = []
        async def enable(**kwargs): pass
        async def dismiss(**kwargs):
            calls.append(kwargs)
            sent.set()
            await response.wait()
        client = NS(send=NS(Page=NS(enable=enable, handleJavaScriptDialog=dismiss)),
                    register=NS(Page=NS(javascriptDialogOpening=lambda callback: registry.register('dialog', callback),
                                        javascriptDialogClosed=lambda callback: registry.register('closed', callback))))
        session = RobustSession()
        watchdog = PopupsWatchdog(browser_session=session, event_bus=session.event_bus)
        watchdog.attach_to_session()
        await session._handle_dialogs(NS(cdp_client=client, session_id='fixture'))
        # The receive loop must return from the callback before it can deliver the command's response.
        await asyncio.wait_for(registry.handle_event('dialog', {'type':'confirm','message':'Submit?'}, 'fixture'), .2)
        await asyncio.wait_for(sent.wait(), .2)
        for _ in range(4):
            await registry.handle_event('dialog', {'type':'confirm','message':'Submit?'}, 'fixture')
        response.set()
        await asyncio.wait_for(asyncio.gather(*list(session._dialog_tasks)), .2)
        self.assertEqual(len(calls), 1)
        await registry.handle_event('closed', {}, 'fixture')
        await registry.handle_event('dialog', {'type':'confirm','message':'Submit?'}, 'fixture')
        await asyncio.wait_for(asyncio.gather(*list(session._dialog_tasks)), .2)
        self.assertEqual(len(calls), 2)
        await session.event_bus.stop(clear=True, timeout=.2)

    async def test_tan_record_completes_lookup_without_an_extra_model_call(self):
        from swarm import browser_agent as ba
        ctx = Context(task={'brief':'', 'access':[]}, board=Blackboard(), llm=None, pool=None)
        records = []
        class Session:
            async def get_current_page_url(self): return 'https://fixture.example/message'
        async def emit(record): records.append(record)
        async def text(session): return 'Your signing TAN is 123ABC'
        tool = ba.build_tools(ctx, 'mail', 'test', emit, stop_on_record='mail.tan').registry.registry.actions['record'].function
        with patch.object(ba, 'read_current_page', text):
            result = await tool(params=ba.RecordParams(kind='mail.tan', data={'tan':'123ABC'}), browser_session=Session())
        self.assertTrue(result.is_done)
        self.assertTrue(result.success)
        self.assertEqual(records[0]['data']['tan'], '123ABC')

    async def test_known_site_role_does_not_wait_for_unrelated_role_discovery(self):
        from swarm.sites import site_for
        class LLM:
            async def ask_json(self, *args, **kwargs): raise AssertionError('Known role needs no model call')
        ctx = Context(task={'brief':'', 'access':[{'siteId':'portal'}]}, board=Blackboard(), llm=LLM(), pool=None)
        ctx.board.results['sites'] = {'tender':'portal'}
        self.assertEqual(await site_for(ctx, 'tender'), 'portal')

    async def test_pagination_saves_first_page_before_next_page_stalls(self):
        from swarm import browser_agent as ba
        ctx = Context(task={'brief':'', 'access':[]}, board=Blackboard(), llm=None, pool=None)
        saved, next_started = [], asyncio.Event()
        class Session:
            async def get_current_page_url(self): return 'https://fixture.example/list'
            async def get_current_page_title(self): return 'Fixture list'
        async def emit(record): saved.append(record)
        async def read(session): return 'First page requirement X with its attributes'
        async def evaluate(session, js, *args):
            next_started.set()
            await asyncio.Event().wait()
        tool = ba.build_tools(ctx, 'fixture', 'test', emit).registry.registry.actions['read_page'].function
        with patch.object(ba, 'read_current_page', read), patch.object(ba, '_evaluate', evaluate):
            task = asyncio.create_task(tool(params=ba.ReadParams(topic='requirements', next_page_link='Next'), browser_session=Session()))
            await asyncio.wait_for(next_started.wait(), 2)
            self.assertEqual(len(saved), 1)
            self.assertIn('requirement X', saved[0]['data']['text'])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(saved), 1)

    async def test_pagination_receipt_counts_all_pages_without_duplicate_evidence(self):
        from swarm import browser_agent as ba
        ctx = Context(task={'brief':'', 'access':[]}, board=Blackboard(), llm=None, pool=None)
        saved, index = [], 0
        class Session:
            async def get_current_page_url(self): return 'https://fixture.example/list'
            async def get_current_page_title(self): return 'Fixture list'
        async def emit(record): saved.append(record)
        async def read(session): return f'Requirement {index} on this page'
        async def evaluate(session, js, *args):
            nonlocal index
            if index == 2: return 'missing'
            index += 1
            return 'clicked'
        tool = ba.build_tools(ctx, 'fixture', 'test', emit).registry.registry.actions['read_page'].function
        with patch.object(ba, 'read_current_page', read), patch.object(ba, '_evaluate', evaluate):
            result = await tool(params=ba.ReadParams(topic='requirements', next_page_link='Next'), browser_session=Session())
        self.assertEqual(len(saved), 3)
        self.assertEqual([e['data']['page_in_read'] for e in saved], [1,2,3])
        self.assertIn('3 pages', result.long_term_memory)
        self.assertIn('LAST page', result.extracted_content)

    async def test_erp_job_does_not_wait_for_unrelated_extraction(self):
        from agents import analyze_materials
        ctx = Context(task={'brief':'', 'access':[]}, board=Blackboard(), llm=None, pool=None)
        slow = Evidence('erp.page', 'erp', {'url':'slow', 'topic':'erp search', 'text':'slow'})
        quick = Evidence('erp.page', 'erp', {'url':'quick', 'topic':'erp search', 'text':'quick'})
        started = asyncio.Event()
        calls = []
        async def extract(ctx, instruction, shape, blocks, **kwargs):
            calls.append(blocks[0])
            if 'slow' in blocks[0]:
                started.set()
                await asyncio.Event().wait()
            return {'materials':[{'material':'M'}]}
        with patch.object(analyze_materials, 'extract', extract):
            pending = asyncio.create_task(analyze_materials.extract_erp(ctx, [slow]))
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(asyncio.gather(
                analyze_materials.extract_erp(ctx, [quick]), analyze_materials.extract_erp(ctx, [quick])), 2)
            self.assertEqual(len(calls), 2)
            self.assertIn('M', ctx.board.get('erp')['materials'])
            self.assertFalse(pending.done())
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_coordinator_cannot_skip_missing_positions_or_unquoted_qa(self):
        from agents.coordinate import CollectionCoordinator, find_gaps
        class LLM:
            async def ask_json(self, *args, **kwargs):
                return {'skipped':['tender positions', {'id':'tender q&a','source':'fixture','quote':'invented absence evidence'}]}
        ctx = Context(task={'brief':'', 'access':[]}, board=Blackboard(), llm=LLM(), pool=None)
        c = CollectionCoordinator(ctx)
        await c.plan([{'id':'tender positions','role':'tender'}, {'id':'tender q&a','role':'tender'}])
        self.assertFalse(c.tried)
        ctx.board.results['tender'] = {'positions':[{'position':'1'}], 'clarifications':[]}
        gaps = find_gaps(ctx.board, {'tender'}, {'tender':['bidder Q&A']})
        self.assertIn('tender q&a', {g['id'] for g in gaps})

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
