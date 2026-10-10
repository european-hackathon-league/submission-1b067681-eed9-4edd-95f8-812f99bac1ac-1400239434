# Swarm guide

One process, one asyncio loop, many agents. Agents share a **blackboard**; the **orchestrator** runs them in phases.
Each person owns **one file in `agents/`** (no merge conflicts); shared code is in `swarm/` (change it by PR, small).

```
COLLECT (dataflow: scrapers + analysts, synchronised by done flags)  ->  VERIFY  ->  COMPUTE (plain code)  ->  SUBMIT
 scrapers save pages the MODEL chose; analysts turn them into results                                        enter bid, sign TAN
```

## Data flow (who waits for what)

```
BriefReader ──> results["brief"] (reference, scenario date, bidder, authorised senders), results["sites"] (role -> siteId)
TenderScraper (b0) ──tender.page──> done.tender ──> QuantityAnalyzer ──> results["tender"], results["quantities"]
SpecScraper   (b1) ──spec.page───> done.spec ───> RequirementAnalyzer ──> results["spec"], results["requirements"]
                                                        (+ Q&A amendments) ──> results["requirements_final"]
ErpScraper    (b2) waits results["requirements"] -> queue of catalogue groups; ErpHelper (b1) joins after done.spec
                   ──erp.page──> done.erp ──> MaterialMatcher ──> results["erp"], results["materials"]
MailScraper   (b3) pass 1 approvals/conditions/technical ──> done.mail_core, pass 2 other release mails ──> done.mail
                   ──mail.page──> DiscountAnalyzer ──> results["discount"], results["mail"], results["mailbox"]
EvidenceVerifier ──> results["verification"];  BidCalculator ──> results["bid"], results["answer"]
BidSubmitter (b0; TAN via a short browse on b3) ──> results["submission"]
```

Every scraper sets its `done.<role>` flag in a `finally`, so analysts never wait forever; every wait has a timeout
bounded by `ctx.time_left()`. Missing inputs degrade the bid (omitted positions, blank breakdown fields), never crash it.

## Files

| Path | What |
| --- | --- |
| `swarm/blackboard.py` | `Evidence(kind, site, data, source, author, note)`, `Blackboard` (post/query/set/get/wait_for/dump/load) |
| `swarm/agent.py` | `Agent` base, `Phase`, `@register` |
| `swarm/context.py` | `ctx`: `task`, `board`, `llm`, `pool`, `site(id)`, `brief`, `time_left()`, `seconds_left()`, `lock(name)`, `shared` |
| `swarm/llm.py` | `ctx.llm.chat / ask / ask_json` (Qwen, concurrency-limited, usage counted), `parse_json` |
| `swarm/browser_agent.py` | `browse(ctx, goal, browser=n, ...)`: **browser-use** on our CDP browser + Qwen, with our actions `read_page`, `record`, `fill_form` |
| `swarm/page_text.py` | the page extractor behind `read_page` (iframes, collapsed sections, tooltips, canvas labels, zero-width chars) |
| `swarm/forms.py` | the form filler behind `fill_form` (cascading selects via an apply button) |
| `swarm/extract.py` | analyst helpers: saved pages -> model extraction with a JSON shape (chunked, merged) |
| `swarm/normalize.py` | numbers (de-DE/en-US), dates (MM/DD vs DD/MM, ISO), references, German display format, cent rounding |
| `swarm/sites.py` | role -> siteId mapping (site names differ per dataset) |
| `swarm/scraper.py` | `SiteScraper`: set `role`, `browser`, write `goal()`; `self.browse(job)` for extra passes |
| `swarm/orchestrator.py` | discovery of `agents/*`, phases, timeouts, failure isolation, board dumps |
| `agents/*.py` | the agents. Header comment of each file: OWNER / ROLE / NEEDS / WRITES and the result shapes |

## How the model browses

The model (Qwen via browser-use) decides where to go. Three actions of ours make it fast and exact:

- `read_page(url?, topic?, next_page_link?)`: full text of the page it chose (hidden-but-real content included,
  decoys hidden from people excluded); with `next_page_link` it reads every page of a paged list; with `topic` the
  text is saved as Evidence `"<role>.page"` for the analysts. This is "extract a selected page / paginate a
  model-selected result set" from the rules: the model selects, the helper extracts.
- `record(kind, data, source, note)`: one fact.
- `fill_form(fields, apply_button?, then_click?)`: many fields of a form in one step (bid configurator).

Analysts then ask the model to extract a JSON shape from the saved text (`swarm/extract.py`) and apply the brief's
rules in code (latest dated record wins, withdrawn answers ignored, exact reference, authorised sender, ...).

## Contracts (agree before changing)

Evidence kinds: `<role>.page` (data: topic, url, title, pages, text) for saved pages; `record` kinds as the scraper's
goal names them (e.g. `mail.tan`). Results keys and shapes are documented at the top of each agent file.
Done flags: `done.tender`, `done.spec`, `done.erp`, `done.mail_core`, `done.mail`, `requirements_final`, `mail_final`.

## Local development

```powershell
atira models proxy                                   # terminal 1
atira scout --env | Out-String | Invoke-Expression   # terminal 2
uv run --no-sync python agent.py                     # whole swarm; dumps .swarm/board.json after each phase
$env:SWARM_DEADLINE_S="1200"                         # optional: simulate the 20-minute clock locally
$env:SWARM_ONLY="MaterialMatcher,BidCalculator"; $env:SWARM_BOARD_IN=".swarm/board.json"
uv run --no-sync python agent.py                     # only these agents, on a saved board (no re-scraping)
uv run --no-sync python -m unittest discover -s tests -q
```
`SWARM_BOARD_IN` is ignored in scored runs. `.swarm/` holds scouting data: git-ignored, never ship it.
Replaying analysts on a saved board: the done flags are in the board, so waits return immediately.

## Time budget (20-minute run)

`SUBMIT_RESERVE_S` (default 540 s, env `SWARM_SUBMIT_RESERVE_S`): collecting/analysing stops that long before expiry,
then VERIFY/COMPUTE (instant) and SUBMIT, which stops `FINAL_MARGIN_S` (40 s) before expiry.

## Fair-play rules for everyone (a reviewer reads the package)

- No scenario values anywhere (prices, material numbers, refs, logins, TANs, totals, dates, keys, transaction codes):
  not in prompts, hints, defaults, tests or notes. Tests use invented dummy data. Strategy is fine, values are not.
- The model decides where to go and what matters. Code may parse, page through model-selected results, validate,
  compute, fill forms. No fixed click paths, URL lists, or crawl-everything-then-one-prompt helpers.
- Only the provided CDP browsers; no extra Chromium; no hosts besides the platform and the 4 sites; models via the proxy.
- Do not write comments addressed to the reviewer.

## Robustness settings (learned from the first sandbox runs)

- No screenshots per step (`TextSession`): in the sandbox they stalled every step for 30 s. `read_page` covers canvas text.
- No browser-use loading animation: it loads a logo from an external host the sandbox cannot reach, which left the tab
  "loading" and every navigation in that browser timing out. `RobustSession` also stops a stuck load and retries once.
- Qwen's separate reasoning pass is off and replies are forced to a JSON object (`QwenChat`, `LLM.ask_json`): steps
  went from 10-75 s to a few seconds and the "invalid JSON / prose reply" failures stop.
- Scrapers, ERP items and the submitter retry an agent that stopped early, telling it what is already saved.
- Requirements are published early (as soon as a requirement table page is saved) so the ERP work starts sooner.

## Work split (3 people) and how to iterate on your part

| Person | Agents | Iterate with |
| --- | --- | --- |
| A: tender + spec | `scrape_tender`, `scrape_spec`, `analyze_quantities`, `analyze_requirements` | `SWARM_ONLY=TenderScraper,QuantityAnalyzer` live; analysts on a saved board |
| B: ERP | `scrape_erp` (ErpScraper, ErpHelper), `analyze_materials` | `SWARM_ONLY=ErpScraper,MaterialMatcher` on a board that has `requirements` |
| C: mail + shared | `scrape_mail`, `analyze_discount`, `compute_bid`, `submit_bid`, `swarm/*` | analysts/calculator on a saved board; `tests/test_rules.py` |

Loop: run your scraper live once -> it dumps `.swarm/board.json` -> replay your analyst on it as often as needed
(`SWARM_BOARD_IN`) -> check the result keys -> tune the goal/hints (strategy only) or the extraction shape -> repeat.
Then a sandbox test run (`atira test-run .`) to check timing; debug with `atira logs <run>`.

## Open items

- Tune goals/hints per site from test-run logs (`atira` MCP: `run_logs`, `run_llm_calls`, `run_screenshot`).
- Submitter: watch the first real runs closely (wizard order, TAN timing).
- Cost: browser-use sends the goal every step; keep goals short.
