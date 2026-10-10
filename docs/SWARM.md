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
TenderScraper (b0) login + overview, then documents (main tab) || Q&A (2nd tab)
                   ──tender.page──> done.tender ──> QuantityAnalyzer ──> results["tender"], results["quantities"]
SpecScraper   (b1) ──spec.page───> done.spec ───> RequirementAnalyzer ──> results["spec"], results["requirements"]
                                                        (+ Q&A amendments) ──> results["requirements_final"]
ErpScraper    (b2) logs in while waiting for results["requirements"]; then small jobs on up to ERP_TABS tabs: one
                   search per catalogue group -> the listed materials that fit -> record -> prices || stock per active material
                   ──erp.page──> done.erp ──> MaterialMatcher ──> results["erp"], results["materials"]
MailScraper   (b3) logs in while the brief is read; one search for the reference, reads every message about the tender
                   ──> done.mail_core; then a search per brief topic still without a valid message (parallel tabs)
                   ──> done.mail ──mail.page──> DiscountAnalyzer ──> results["discount"], results["mail"], results["mailbox"]
CollectionCoordinator (reasoning model) as each site's first pass + analysis is done: code lists the gaps (facts the
                   bid needs that no saved page holds), the model turns them into focused follow-up jobs on that site,
                   the affected analyses run again ──> results["gaps"] (what is still open when COLLECT ends)
PopupGuard (next to every browse): clears cookie banners, surveys, notices, error pages, access gates
EvidenceVerifier ──> results["verification"];  BidCalculator ──> results["bid"], results["answer"]
BidSubmitter (b0 main tab, full reasoning; TAN via a short browse on b3's main tab) ──> results["submission"]
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
| `swarm/browser_agent.py` | `browse(ctx, goal, browser=n, tab=..., flash=..., careful=...)`: **browser-use** on our CDP browser + Qwen, with our actions `read_page`, `record`, `fill_form`, `share_tip`, `read_document`, `read_visual`; `Tab` |
| `swarm/guard.py` | `PopupGuard`: runs next to every browsing agent and clears noise (model decides once per kind of popup, cached per site) |
| `swarm/page_text.py` | the page extractor behind `read_page` (iframes, collapsed sections, tooltips, canvas labels, zero-width chars) |
| `swarm/forms.py` | the form filler behind `fill_form` (cascading selects via an apply button) |
| `swarm/extract.py` | analyst helpers: saved pages -> model extraction with a JSON shape (chunked, merged) |
| `swarm/normalize.py` | numbers (de-DE/en-US), dates (MM/DD vs DD/MM, ISO), references, German display format, cent rounding |
| `swarm/sites.py` | role -> siteId mapping (site names differ per dataset) |
| `swarm/scraper.py` | `SiteScraper`: set `role`, `browser`, write `goal()`; `self.browse(job)` for extra passes, `self.browse_in_tabs(jobs)` for parallel ones; `run_job(ctx, role, job)` for the coordinator |
| `swarm/orchestrator.py` | discovery of `agents/*`, phases, timeouts, failure isolation, board dumps |
| `agents/*.py` | the agents. Header comment of each file: OWNER / ROLE / NEEDS / WRITES and the result shapes |

## How the model browses

The model (Qwen via browser-use) decides where to go. Three actions of ours make it fast and exact:

- `read_page(url?, topic?, next_page_link?)`: full text of the page it chose (hidden-but-real content included,
  decoys hidden from people excluded); with `next_page_link` it reads every page of a paged list; with `topic` the
  text is saved as Evidence `"<role>.page"` for the analysts. This is "extract a selected page / paginate a
  model-selected result set" from the rules: the model selects, the helper extracts.
- `record(kind, data, source, note)`: one fact.
- `fill_form(fields, apply_button?, then_click?)`: many fields of a form in one step (bid configurator). Also custom
  toggles without an input (shadow DOM), named by their text; sliders set to 'max' are dragged with real mouse events.
  (browser-use's `evaluate` is off: writing page JavaScript cost whole step budgets.)
- `share_tip(tip)`: a short how-to for the site (a URL pattern that opens results directly, how a record opens); later
  agents on that site get the tips in their hints, so one agent's discovery saves every other agent the search.

`read_page` reads live content by default and preserves URL fragments. Explicit `refresh=false` permits cached
immutable evidence. Failed extractions are retried rather than cached as empty results. The popup guard (`swarm/guard.py`) sweeps the agent's page before every step and every 2 s:
each new kind of dialog / overlay / banner / short error page goes to the model once ("click" noise, "fill" an access
gate, "leave" anything an agent works with), and the decision is cached per site for all agents, so a notice that comes
back on every page costs one model call per run. The submitter's guard is careful: clicks on noise without inputs only.
Links that open another page ("accept and continue" in notices, "Öffnen" in download bars) are never clicked: when the
model picks one, the item's single staying button (its x) is used instead; only an error page's retry link may lead
away. Inline gates (a slider to drag to the end, a robot checkbox) are found by their slider and unlocked on every
page; fills that only tick boxes and move sliders are cached like clicks. `read_page` prints list links as `<url>`, so
agents open records and messages by URL instead of clicking through locked or paged lists.

Analysts then ask the model to extract a JSON shape from the saved text (`swarm/extract.py`) and apply the brief's
rules in code (latest dated record wins, withdrawn answers ignored, exact reference, authorised sender, ...).

## Contracts (agree before changing)

Evidence kinds: `<role>.page` (data: topic, url, title, pages, text) for saved pages; `record` kinds as the scraper's
goal names them (e.g. `mail.tan`). Results keys and shapes are documented at the top of each agent file.
Done flags: `done.tender`, `done.spec`, `done.erp`, `done.mail_core`, `done.mail`, `requirements_final`, `mail_final`.
Analysts expose `refresh(ctx)` (module level): re-run on the pages saved so far (ERP and mail pages are extracted once
each and cached), so the coordinator can update results after its follow-up jobs.

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

`SUBMIT_RESERVE_S` (default 600 s, env `SWARM_SUBMIT_RESERVE_S`): collecting/analysing stops that long before expiry,
then VERIFY/COMPUTE (instant) and SUBMIT, which stops `FINAL_MARGIN_S` (40 s) before expiry.
`RUN_LIMIT_S` (default 1200 s, env `SWARM_RUN_LIMIT_S`, 0 = off): every run plans with at most 20 minutes from process
start, so 2-hour test runs show the timing of a scored run.

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

## Speed (each browsing step is one model call of ~14 s, most of it queue wait: fewer sequential steps = faster)

- Parallel tabs: the 4-browser limit does not limit tabs. Independent jobs run in tabs of their own (`Tab`,
  `browse_in_tabs`); tabs of one browser share its login cookies (most sites; the webmail asks each tab to log in).
  New tabs open next to the others in the browser's window (`TAB_WINDOWS=1`: a window each) and close when their job
  ends. An agent sees only its own tab and unowned ones (popups), never another agent's tab. Rule: one browse() per
  tab at a time.
- Every agent tab has focus emulation on: only one window/tab has the real focus, and without it typing and
  focus-dependent page scripts silently did nothing in the others.
- Lifecycle events are routed per tab (browser-use's single per-client handler dropped other tabs' load events, so
  each navigation in a second tab waited 3-8 s). A tab's later jobs continue from its current page.
- Flash mode for navigating/extracting agents (default): replies are memory + actions only, with a compact system
  prompt (~0.6k instead of ~5k tokens) and a 2000-token reply cap. The bid submitter keeps full reasoning (`flash=False`).
- Unused browser-use actions (web search, LLM extract, screenshots, file system, uploads) are excluded: smaller schema
  in every prompt, and no wasted steps on them.
- Prompt text (goals, hints, rules, tool descriptions) is written tersely: every word is resent on every step.
- Small jobs instead of big ones: an ERP job per search / material record / prices / stock finishes inside its step
  budget, and materials the search list already rules out are never opened.
- Logs: browser-use's own step logs are off (the platform keeps only the tail of a run's log, and they pushed the
  swarm's lines out); `browse` logs one line per step, and the board is summarised in one line after COLLECT.

## Work split (3 people) and how to iterate on your part

| Person | Agents | Iterate with |
| --- | --- | --- |
| A: tender + spec | `scrape_tender`, `scrape_spec`, `analyze_quantities`, `analyze_requirements` | `SWARM_ONLY=TenderScraper,QuantityAnalyzer` live; analysts on a saved board |
| B: ERP | `scrape_erp` (ErpScraper + its tab workers), `analyze_materials` | `SWARM_ONLY=ErpScraper,MaterialMatcher` on a board that has `requirements` |
| C: mail + shared | `scrape_mail`, `analyze_discount`, `compute_bid`, `submit_bid`, `swarm/*` | analysts/calculator on a saved board; `tests/test_rules.py` |

Loop: run your scraper live once -> it dumps `.swarm/board.json` -> replay your analyst on it as often as needed
(`SWARM_BOARD_IN`) -> check the result keys -> tune the goal/hints (strategy only) or the extraction shape -> repeat.
Then a sandbox test run (`atira test-run .`) to check timing; debug with `atira logs <run>`.

## Open items

- Tune goals/hints per site from test-run logs (`atira` MCP: `run_logs`, `run_llm_calls`, `run_screenshot`).
- Submitter: watch the first real runs closely (wizard order, TAN timing).
- Cost: browser-use sends the goal every step; keep goals short.


## Workflow safeguards and evaluation

Requirements are published as specification pages arrive; ERP keeps accepting new requirements until final analysis.
Mail stops 150 seconds before the submission reserve, and every completed mail/ERP extraction is retained immediately.
At the collection cutoff the orchestrator reconciles completed extractions without another model call.

Model attempts time out after 55 seconds (`SWARM_MODEL_TIMEOUT_S`); browsing and analyst calls share a concurrency
limit of 16. A run has its own monotonic clock. Collection children and guard tasks are cancelled and awaited before
signing. Local runs launch persistent CDP browsers so a resumed wizard keeps its tab and login.

The submitter starts a background TAN lookup and polls it while retaining the portal wizard. A replacement request
explicitly invalidates that lookup. A model `done` result is insufficient: `confirm_submission` must match a quoted
receipt on the tender portal. Missing confirmation produces a failed run rather than a misleading success exit.

Open shadow roots, visible editable controls, PDF/text/ZIP documents, and explicit visual transcription have helper
support. Failed or unverified form fields prevent the helper's continue click. These mechanisms do not guarantee
compatibility with every framework or website. [The review backlog](REVIEW.md) distinguishes fixes from remaining
risks; `scripts/evaluate_navigation.py` runs an opt-in live-model discovery check against two synthetic layouts.
