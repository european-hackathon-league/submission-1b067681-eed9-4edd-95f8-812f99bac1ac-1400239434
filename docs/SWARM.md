# Swarm guide

One process, one asyncio loop, many agents. Agents share a **blackboard**; the **orchestrator** runs them in phases.
Each person owns **one file in `agents/`** (no merge conflicts); shared code is in `swarm/` (change it by PR, small).

```
COLLECT (scrapers, 1 per site, own browser) -> ANALYZE -> VERIFY -> COMPUTE (plain code) -> SUBMIT (always runs)
        post Evidence                           read board, write results[...]                 enter bid + sign TAN
```

## Files

| Path | What |
| --- | --- |
| `swarm/blackboard.py` | `Evidence(kind, site, data, source, author, note)`, `Blackboard` (post/query/set/get/wait_for/dump/load) |
| `swarm/agent.py` | `Agent` base, `Phase`, `@register` |
| `swarm/context.py` | `ctx`: `task`, `board`, `llm`, `pool`, `site(id)`, `brief`, `time_left()`, `seconds_left()` |
| `swarm/llm.py` | `ctx.llm.chat / ask / ask_json` (Qwen, concurrency-limited, usage counted), `parse_json` |
| `swarm/browser_agent.py` | `browse(ctx, goal, browser=n, ...)`: wraps the **browser-use** framework (our CDP browser, Qwen proxy, telemetry off, only the task's sites allowed, `record` action -> blackboard) |
| `swarm/scraper.py` | `SiteScraper`: subclass, set `site_id`, `browser`, write `goal()` |
| `swarm/orchestrator.py` | discovery of `agents/*`, phases, timeouts, failure isolation, board dumps |
| `agents/*.py` | the agents. Header comment of each file: OWNER / ROLE / NEEDS / WRITES |

Starting agents (all stubs except the 4 scrapers, which run a generic goal): `scrape_tender`, `scrape_spec`, `scrape_erp`,
`scrape_mail`, `analyze_quantities`, `analyze_requirements`, `analyze_materials`, `analyze_discount`, `verify_evidence`,
`compute_bid`, `submit_bid`. Add a new agent = add a file with `@register` (nothing else to wire).

## Adding / changing an agent

```python
@register
class MyAgent(Agent):
    phase = Phase.ANALYZE
    async def run(self):
        positions = self.ctx.board.query("tender.position")        # list[Evidence]
        out = await self.ctx.llm.ask_json("system...", "user...")   # model decides; code validates
        await self.ctx.board.set("quantities", out)                 # named result for downstream agents
```
Browsing agent: `await browse(self.ctx, goal, browser=self.browser, site_id=..., on_record=...)` (browser-use). Run ONE browse per browser index at a time (max 4 browsers: 0-3). Raw Playwright is still available via `self.ctx.pool.new_page(n)` if you need it.
Need an upstream result within the same phase: `await self.ctx.board.wait_for(key="requirements", timeout=...)`.
Always check `self.ctx.time_left()` in loops. Raise on failure: it is logged on `board.errors`, others carry on.

## Contracts (agree before changing)

Evidence `kind` names (producer -> consumer): `tender.meta|position|clarification`, `spec.revision|requirement|amendment`,
`erp.material|stock|price_condition|other`, `mail.discount_approval|tan|other`. Keep `data` = facts as shown on the page
(strings as found, with dates and sender), interpretation happens in analyzers. Put caveats in `note`.
Results keys: `quantities`, `requirements`, `materials`, `discount`, `verification`, `bid`, `answer`. **Document the shape of
each result you write at the top of your agent file** so consumers can rely on it; change shapes only after telling the consumer.
If your upstream data is missing, degrade (partial bid) rather than crash: a signed partial bid beats nothing.

## Local development

```powershell
atira models proxy                                   # terminal 1
atira scout --env | Out-String | Invoke-Expression   # terminal 2
uv run --no-sync python agent.py                     # whole swarm; dumps .swarm/board.json after each phase
$env:SWARM_ONLY="MaterialMatcher"; $env:SWARM_BOARD_IN=".swarm/board.json"
uv run --no-sync python agent.py                     # only your agent, on a saved board (no re-scraping)
uv run --no-sync python -m unittest discover -s tests -q
```
`SWARM_BOARD_IN` is ignored in scored runs. `.swarm/` holds scouting data: git-ignored, never ship it.
Agents can be unit tested with a `Context(task, Blackboard(), fake_llm, None)`; see `tests/test_swarm.py`.

## Framework: browser-use

Browsing agents use [browser-use](https://github.com/browser-use/browser-use) (pinned in `pyproject.toml`; also `pydantic-settings`,
which it forgets to declare). `swarm/browser_agent.py` is the only place that touches it: telemetry, cloud sync and version check
are switched off via env (they call external hosts), navigation is restricted to the task's site URLs, extensions are off, and it
attaches to the run's CDP browsers (never launches its own in a run). Qwen settings (`make_llm`) may need tuning on first runs:
schema in prompt, no forced structured output, no frequency penalty. Analyzers/verifiers do NOT need it: use `ctx.llm`.

## Fair-play rules for everyone (a reviewer reads the package)

- No scenario values anywhere (prices, material numbers, refs, logins, TANs, totals, dates): not in prompts, defaults, tests or notes.
  Tests use invented dummy data. Strategy ("latest approval for this exact tender wins") is fine, values are not.
- The model decides where to go and what matters. Code may parse, page through model-selected results, validate, compute.
  Do not write fixed click paths, URL lists, or crawl-everything-then-one-prompt helpers.
- Only the provided CDP browsers; no extra Chromium; no hosts besides the platform and the 4 sites; models via the proxy.
- Do not write comments addressed to the reviewer.

## Not yet done (first tasks)

- `browse()` is untested end to end (builds and attaches in a smoke test only): the first scout run will need Qwen/prompt
  tuning (tool-call format, iframes, popups, ERP transaction codes). Owner of each scraper tunes their `goal()`/`hints`.
- Time: phases COLLECT-COMPUTE are cut at `time_left() <= 0` (180 s before expiry); SUBMIT then gets until 60 s before
  expiry (hard timeout). The submitter should enter data progressively so a partial signed bid exists early.
- Token/cost budget per agent is not tracked yet (only totals in `ctx.llm`).
