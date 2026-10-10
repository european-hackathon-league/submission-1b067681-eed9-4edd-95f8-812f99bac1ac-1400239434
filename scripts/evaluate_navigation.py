"""Opt-in live-model navigation evaluation against synthetic, unseen local UI variants.

Start `atira models proxy`, then run this script. No production data or credentials.
This tests discovery/extraction, not the full RFQ scoring contract.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QWEN_BASE_URL', 'http://127.0.0.1:8765/qwen/v1')
os.environ.setdefault('QWEN_API_KEY', 'local')
os.environ['HEADLESS'] = '1'

from playwright.async_api import async_playwright
from swarm.blackboard import Blackboard
from swarm.browser_agent import browse, _MAIN_TABS, _OWNERS, _OPENERS
from swarm.browser_pool import BrowserPool
from swarm.context import Context
from swarm.llm import LLM


async def evaluate(shadow):
    active, archive = uuid4().hex[:10], uuid4().hex[:10]
    expected = 'REQ-' + uuid4().hex[:8].upper()
    pages = {
        '/': '<h1>Vendor workspace</h1><a href="/library">Engineering library</a>',
        '/library': f'<h1>Specifications</h1><a href="/{archive}">Revision A — superseded</a>'
                    f'<a href="/{active}">Revision C — current, binding</a>',
        '/'+archive: '<h1>Superseded revision A</h1><p>Old values, do not use.</p>',
        '/'+active: f'<h1>Revision C — current</h1><table><tr><th>Requirement</th><th>Bore</th></tr>'
                    f'<tr><td>{expected}</td><td>73 mm</td></tr></table>',
    }
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = pages.get(self.path, '<h1>Not found</h1>')
            if shadow:
                html = '<x-workspace></x-workspace><script>document.querySelector("x-workspace").attachShadow({mode:"open"}).innerHTML=' + json.dumps(html) + ';</script>'
            data = ('<!doctype html><meta charset="utf-8"><title>Engineering workspace</title>' + html).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    task = {'brief':'Find the current technical requirements.', 'access':[{'siteId':'opaque','url':url}],
            'expires_at':(time.time()+180)*1000}
    _MAIN_TABS.clear()
    _OWNERS.clear()
    _OPENERS.clear()
    started = time.monotonic()
    async with async_playwright() as pw:
        pool, llm = BrowserPool(pw), LLM()
        ctx = Context(task=task, board=Blackboard(), llm=llm, pool=pool)
        records = []
        async def save(record): records.append(record)
        try:
            await pool.prepare()
            out = await browse(ctx, f'Open {url}. Find the current binding technical specification. '
                               'Save its requirement table with read_page and a topic. Ignore superseded values. '
                               'Finish only when the requirement identifier and bore are saved.',
                               browser=0, site_id='opaque', on_record=save, author='NavigationEvaluation',
                               max_steps=10, stop_reserve=0)
            saved = any(expected in r['data'].get('text','') and '73' in r['data'].get('text','') for r in records)
            return {'layout':'shadow' if shadow else 'plain', 'passed':saved and out['done'],
                    'evidence_saved':saved, 'steps':out['steps'], 'seconds':round(time.monotonic()-started,1),
                    'model_calls':llm.calls}
        finally:
            await pool.close()
            if llm._client: await llm._client.close()
            server.shutdown()
            server.server_close()


async def main():
    results = []
    for shadow in (False, True):
        results.append(await evaluate(shadow))
        print('EVALUATION ' + json.dumps(results[-1]), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
