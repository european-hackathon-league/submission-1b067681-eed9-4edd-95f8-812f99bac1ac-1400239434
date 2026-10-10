"""Opt-in model-driven signing regression on synthetic local sites (no challenge data).

Start `atira models proxy`, then run with uv. This validates signing only, not RFQ pricing.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QWEN_BASE_URL', 'http://127.0.0.1:8765/qwen/v1')
os.environ.setdefault('QWEN_API_KEY', 'local')
os.environ['HEADLESS'] = '1'

from playwright.async_api import async_playwright
from agents.submit_bid import BidSubmitter
from swarm.blackboard import Blackboard
from swarm.browser_agent import _MAIN_TABS, _OWNERS, _OPENERS
from swarm.browser_pool import BrowserPool
from swarm.context import Context
from swarm.llm import LLM
from swarm import guard


def gate(label, content):
    key = uuid4().hex
    return f'''<section aria-label="{label}">
      <label>Zum Entsperren ganz nach rechts ziehen<input type="range" min="0" max="100" value="0"></label>
      <label><input type="checkbox">Ich bin kein Roboter</label><span role="status">Gesperrt</span>
      <div id="{key}" hidden>{content}</div>
      <script>(()=>{{const box=document.getElementById('{key}').parentElement;
      const update=()=>{{const open=box.querySelector('[type=range]').value==='100' && box.querySelector('[type=checkbox]').checked;
      box.querySelector('div').hidden=!open;box.querySelector('[role=status]').textContent=open?'Entsperrt':'Gesperrt';}};
      box.addEventListener('input',update);box.addEventListener('change',update);}})();</script>
      </section>'''


async def evaluate():
    reference = 'FIXTURE-' + uuid4().hex[:8].upper()
    wizard, review, letter, receipt = ['/' + uuid4().hex for _ in range(4)]
    state = {'tan': '', 'accepted': False, 'conditions': False}

    class Handler(BaseHTTPRequestHandler):
        def send_html(self, body):
            data = ('<!doctype html><html lang="de"><meta charset="utf-8"><title>Fixture workspace</title>'
                    '<style>body{font:16px sans-serif;max-width:900px;margin:32px}label{display:block;margin:12px}section{border:1px solid;padding:16px;margin:12px}</style>'
                    f'<body>{body}</body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, target):
            self.send_response(303)
            self.send_header('Location', target)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            if self.path == '/':
                body = f'<h1>Vergabeportal</h1><p>Verfahren {reference}</p><a href="{wizard}">Angebotsabgabe</a>'
            elif self.path == wizard:
                body = (f'<h1>Konditionen — {reference}</h1><form method="post" action="{wizard}">'
                        '<label>Rabatt (%)<input name="discount"></label><label>Nettosumme EUR<input name="total"></label>'
                        '<span role="group" aria-label="Betragsart"><output aria-label="Betragsart: Nettosumme manuell bestätigen">'
                        'Nettosumme manuell bestätigen</output></span><p>Zahlenformat: 0,00</p><button>Weiter</button></form>')
            elif self.path == review:
                body = (f'<h1>Prüfung und Signatur — {reference}</h1><p>Teilangebot: keine Positionen. Rabatt 0,00 %. Nettosumme 0,00 EUR.</p>'
                        '<form method="post" action="/request"><button>Signatur-TAN anfordern</button></form>'
                        f'<p>{"TAN angefordert; neueste Nachricht im Postfach lesen." if state["tan"] else "Noch keine TAN angefordert."}</p>'
                        '<form method="post" action="/sign" onsubmit="return confirm(\'Angebot jetzt verbindlich abgeben?\')">'
                        '<label>Signatur-TAN<input name="tan"></label><button>Verbindlich abgeben</button></form>')
            elif self.path == '/mail':
                link = f'<a href="{letter}">Neueste Signatur-TAN für {reference}</a>' if state['tan'] else '<p>Keine TAN-Nachricht.</p>'
                body = '<h1>Postfach</h1>' + gate('Nachrichtenliste', link) + gate('Lesebereich', '<p>Nachricht aus der Liste öffnen.</p>')
            elif self.path == letter:
                body = f'<h1>Signatur-TAN für {reference}</h1><p>Neueste Anforderung</p>' + gate(
                    'Nachricht', f'<p>Ihre Signatur-TAN für {reference}: {state["tan"]}</p>')
            elif self.path == receipt and state['accepted']:
                body = f'<h1>Angebot erfolgreich eingereicht</h1><p>Das signierte Angebot für {reference} wurde verbindlich abgegeben.</p>'
            else:
                body = '<h1>Seite nicht gefunden</h1><a href="/">Portal</a>'
            self.send_html(body)

        def do_POST(self):
            data = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode())
            if self.path == wizard:
                if data.get('discount') == ['0,00'] and data.get('total') == ['0,00']:
                    state['conditions'] = True
                    return self.redirect(review)
                return self.send_html(f'<p>Ungültiges Format. Erforderlich: 0,00.</p><a href="{wizard}">Konditionen korrigieren</a>')
            if self.path == '/request' and state['conditions']:
                state['tan'] = uuid4().hex[:8].upper()
                return self.redirect(review)
            if self.path == '/sign' and state['conditions'] and state['tan'] and data.get('tan') == [state['tan']]:
                state['accepted'] = True
                return self.redirect(receipt)
            return self.send_html(f'<p>Keine Abgabe: TAN ungültig.</p><a href="{review}">Zur Signatur</a>')

        def log_message(self, *args): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    task = {'brief':'Submit the prepared partial bid. Use the mailbox for signing TAN.',
            'submission':{'kind':'site'}, 'expires_at':(time.time()+480)*1000,
            'access':[{'siteId':'portal','url':url+'/'}, {'siteId':'mail','url':url+'/mail'}]}
    board = Blackboard()
    board.results.update({'sites':{'tender':'portal','mail':'mail'}, 'brief':{'tender_reference':reference},
        'bid':{'answer':{'decision':'bid','tender_reference':reference,'discount_percent':0,'positions':[], 'total_net':0},
               'lines':[], 'total_display':'0,00'}})
    _MAIN_TABS.clear(); _OWNERS.clear(); _OPENERS.clear()
    guard._DECISIONS.clear(); guard._PENDING.clear()
    started = time.monotonic()
    try:
        async with async_playwright() as pw:
            pool, llm = BrowserPool(pw), LLM()
            ctx = Context(task=task, board=board, llm=llm, pool=pool)
            try:
                await pool.prepare()
                await BidSubmitter(ctx).run()
                result = {'passed':state['accepted'] and bool((board.get('submission') or {}).get('accepted')),
                          'conditions_accepted':state['conditions'], 'server_accepted':state['accepted'],
                          'receipt_recorded':bool(board.get('submission')), 'model_calls':llm.calls,
                          'seconds':round(time.monotonic()-started, 1)}
                print('SIGNING_EVALUATION '+json.dumps(result), flush=True)
                return result['passed']
            finally:
                tasks = list(guard._TASKS)
                for t in tasks: t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await pool.close()
                if llm._client: await llm._client.close()
    finally:
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(evaluate()) else 1)
