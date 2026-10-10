@AGENTS.md

# Project guide: Atira EHL challenge agent ("From RFQ to bid")

A Python agent that, at run time, uses a model (hosted Qwen) to navigate four websites (tender portal, supplier portal,
ERP, webmail), work out the task from the brief, and submit one answer. Brief: `atira challenge rfq`.
Platform docs: https://ehl-challenge.atira.ai/start and `https://ehl-challenge.atira.ai/docs/run-contract.md`.

## Layout

| File | Purpose |
| --- | --- |
| `agent.py` | Entry point. Write the agent in `solve(task, page)`. |
| `run.py` | Run contract: `load_run`, `submit_answer`, `seconds_left`. Platform plumbing, change rarely. |
| `browsers.py` | Attaches to the run's browsers (CDP); locally starts Chromium. |
| `models.py` | `ask_qwen`, `ask_clef`, `qwen()` (OpenAI SDK client). |
| `agent.json` | Sandbox config: `setup`, `run`, `models` (hosted models to wait for), optional `"browsers": 1-4`. |
| `swarm/`, `agents/` | Agent-swarm framework and one file per agent: see `docs/SWARM.md` (read before adding an agent). |
| `tests/` | Unit tests against fakes. |

`task` has `brief`, `access` (site ids, URLs, credentials), `submission` (`api` or `site`) and `answer_schema`.
For `api` submissions return the answer from `solve()`; for `site` submissions enter it on the website and return `None`.

## Rules that decide whether a submission is accepted

- A model must pick navigation targets and make decisions at runtime. No hardcoded answers, fixed scripts, crawlers
  or copied values. A coding agent reviews every package. Helpers (parsing, pagination of a model-selected result set,
  validation, form filling) are fine.
- A scored run lasts 20 min. Submit the best answer about 1 min before the end (`run.seconds_left(task)`); late = 0.
- Max 4 browsers, 4 vCPU / 8 GB RAM. The sandbox only reaches the platform and the websites.
- Package: max 20 MB compressed, `agent.json` at the root. Never include `.env` or keys (they are git-ignored).
- Hosted models are free, but each call adds simulated cost to ranking (quality, then cost x time). Use Qwen for
  easy steps; first call after idle can take several minutes, so use long timeouts.

## Setup per laptop (once)

Prerequisites: Git, Python is NOT required (uv manages 3.12), and a GitHub account that is on the Atira team.

### Windows (PowerShell)
```powershell
irm https://ehl-challenge.atira.ai/install.ps1 | iex          # Atira CLI (read the script first if you prefer)
winget install --id astral-sh.uv -e                           # uv
# open a NEW terminal so PATH updates
atira login                                                   # browser sign-in with GitHub
uv sync
uv run playwright install chromium
```

### macOS / Linux
```sh
curl -fsSL https://ehl-challenge.atira.ai/install.sh | sh     # Atira CLI; then add ~/.local/bin to PATH
curl -LsSf https://astral.sh/uv/install.sh | sh               # uv (or: brew install uv)
# open a NEW terminal
atira login
uv sync
uv run playwright install chromium        # on Linux add: uv run playwright install-deps chromium
```

### Both: Claude Code helpers (optional)
```sh
claude mcp add atira -- atira mcp     # lets Claude read run logs, model calls, timings, compare runs
```
Check with `atira whoami`. Use `atira login --device` if no browser is available.

## Develop locally

Two terminals, both in the repo root.

Terminal 1 (leave open): the free-model proxy.
```sh
atira models proxy
```

Terminal 2: load scouting env, then run the agent.
```powershell
# Windows PowerShell
atira scout --env | Out-String | Invoke-Expression
uv run --no-sync python agent.py
```
```sh
# macOS / Linux
eval "$(atira scout --env)"
uv run --no-sync python agent.py
```
`atira scout --env` sets `CHALLENGE_SCOUTING`, `CHALLENGE_SITE_ACCESS`, site URLs and the `QWEN_*` / `CLEF_*`
variables pointing at the local proxy. The variables only live in that terminal. Local runs use the public
scouting logins: nothing done there is scored, and answers entered on the tender portal never count. Local model calls
are free and unranked. `atira scout` (no flag) prints the logins and brief for manual browsing.

Unit tests: `uv run --no-sync python -m pytest tests -q` (add pytest with `uv add --dev pytest` if missing).

## Test in the real sandbox

```sh
atira test-run . --follow        # same sandbox as scored runs, public dataset, up to 2 h, not ranked
atira test-run . --dry-run       # only shows what would be packed (no upload)
```
Max 3 test runs waiting at once. Inspect results:
```sh
atira status                     # latest submissions and test runs
atira logs <run-id> --follow     # reh_... (test run) or run_... (scored run)
atira package <id>               # what was uploaded
atira cancel <run-id>
```
With the `atira` MCP server Claude can also fetch logs by time, model calls, where time went, browser screens and
compare two runs. Ask it "why did run reh_xxx fail?".

## Submit (ranked, public)

Run a test run first and make sure it finishes with a valid answer. Then agree in the team who submits
(one submission in flight per team, and it is public and final).
```sh
atira submit . --dry-run         # check the package
atira submit . --follow          # review -> one scored run on the public dataset -> leaderboard
atira leaderboard
```
The final ranking uses a private dataset with different websites and data, so build a general agent that does not
rely on public-dataset specifics. Choose the final attempt with `atira final [--best-of 1|3]` before the final run
starts (own model keys cost up to 3x).

## Models

- Default: hosted `qwen` (Qwen3.8-Flash-Next, up to 32 concurrent calls/team) via `models.qwen()`; `clef` (image
  decision model, 16 concurrent) via `models.ask_clef`. List every hosted model used in `agent.json` `"models"`.
- Own provider keys (Anthropic, OpenAI, ...) are stored as team secrets on the team page and go through the run's
  metering proxy. See the run contract, section LLM calls.
- Claude Code (this assistant) is only for writing code and runs on your own Claude. Optional experimental launcher
  `claude-atira` (Windows, per-user setup, not in the repo) points Claude Code at Qwen; plain `claude` is the default.

## Team workflow

- Commit with normal `git commit` / `git push` so Entire checkpoints get linked. Do not push `entire/*` branches.
- Never read, print or commit `.env` or API keys. `.entire/settings.json` is shared; `settings.local.json` is personal.
- Pull before starting, keep commits small, and say in the team chat before running `atira submit`.

## Challenge overview: `rfq` v6 (source: challenge page, fair-play.md, run-contract.md; checked 2026-10-10)

Goal: turn a public tender (German hydraulics manufacturer, hydraulic cylinders) into one correct, priced, **signed** bid
without human help. Answer is entered in the **tender-portal** configurator (`submission.kind = site`), signed with a TAN
from the webmail. Limits: answer 65,536 B, up to 100 positions, notes 4,000 chars.

### The four sites (each run has its own logins; public scouting logins are unscored)
| Site | Holds | Traps |
| --- | --- | --- |
| `tender-portal` | tender, bill of quantities, bidder Q&A (changes quantities), bid configurator | cookie wall, flaky error pages, docs behind expression of interest, iframes, ZIPs, deadline in other timezone |
| `customer-portal` | buyer's technical spec + revisions | opens OLDEST revision first, nested viewers, paged tables |
| `erp` | materials, stock, lead times, price conditions | SAP-style transaction codes, F4 search popup, status codes, short session timeout (re-login) |
| `mail` | sales inbox: director's discount approval, signing TAN | look-alike/superseded approvals, archive folder, paging |

### Bid rules (29 in the brief; the brief in `task["brief"]` is authoritative, re-read it every run)
- Quantity = tender qty, overridden by the latest clarification per position; withdrawn answers ignored.
- Requirement = spec revision marked *current*, with scoped amendments applied.
- Material = active ERP material matching bore, stroke, pressure, mounting, certification EXACTLY; none -> "no bid".
- Price = valid price condition on scenario date at highest reached quantity scale, minus approved discount, round half-up
  to the cent, converted to EUR. Discount = latest sales-director approval for this exact tender (conditional thresholds), else 0%.
- Lead time = stock if it covers qty, else production lead time; shared stock is all-or-nothing per material.
- Konditionsgruppen decide if discount hits base only / base + mass surcharge / neither. Setup fees never discounted.
- Packaging cartons, shared setup/pool fees, delivery commitments allocated per position; project bonus = marginal
  percentage bands allocated proportionally in cents; final price formula rounds ONCE at the end.
- Metal-index rates selected by date; nominal stroke derived from reserves.
- Authority: only authorised senders + exact tender reference; newest valid record wins.
- Partial submission allowed (explicitly omitted positions) but bid must still be signed. Missing material -> no bid.

### Scoring / ranking
- Quality 0-100: decision 5, reference 5, discount 5, positions 75 (minus extra checks), total 5, arithmetic consistency 5;
  +5 each for withdrawn-quantity and metal-index checks when present. Per position: material 20%, quantity 15%,
  base price 15%, surcharge 10%, packaging 10%, project bonus 10%, final unit price 10%, delivery 10%.
- Correct tender reference is mandatory (else ineligible, ranked after all eligible).
- Order: eligible -> quality (higher) -> cost x time (lower; cost = simulated model prices, time = wall clock to accepted answer).
- Final ranking: PRIVATE dataset first, then public, then "system elegance". Private = same jobs/rules/format but different
  software, layouts, names, content, data. Do NOT tune to public-1; derive everything from the sites at runtime.
- Private run uses latest passing submission (or best of 3 if opted in via `atira final --best-of 3`).

### Fair play (an AI reviewer reads the whole package: passed / flagged / rejected)
Forbidden: any scenario value in the package (code, config, prompts, notes, defaults, fallbacks: prices, material numbers,
totals, discounts, tender refs, logins, TANs); pre-solving the answer (yourself or with an assistant) and shipping it;
fixed click paths; crawl-everything-then-one-model-call; model only as fallback; contacting hosts other than platform + 4 sites;
models outside the proxy; local/bundled models; starting own browsers (even headless; extras are killed); attacking platform/sites;
sharing scored-run data. Never write notes addressed to the reviewer (steering counts against us).
Allowed: any agent design, any provider via the proxy with team-secret keys, own code/prompts/tools, OSS libs installed in
`setup`, knowledge from scouting/test runs (as general strategy, never as values), helper code for browser control, parsing,
arithmetic, validation, retries, form filling, paging through results the model selected.
Helpers MAY NOT discover and traverse the sites to assemble evidence: the model decides where to go, what to search, what matters.

### Building an agent swarm
- Multiple agents/processes/threads are explicitly allowed; no limit on agent count, only **max 4 browsers** (`"browsers": n`
  in `agent.json`, CDP urls in `CDP_URLS`, browser n = display `:n`, CDP port 9221+n). Tabs/contexts inside them are unlimited.
- Frameworks: nothing is whitelisted or banned; any open-source library (respect licence) installed during `setup`
  (network allowed there, no credentials). Toolchains: Python 3.12 + uv, Node 24 (+pnpm, bun, runs .ts), Rust/cargo, gcc, make, git, curl, jq.
  Run phase is offline except platform + sites + model proxy. Package <= 20 MB. Plain asyncio/threads + OpenAI SDK is enough;
  agent frameworks (LangGraph, OpenAI Agents SDK, smolagents, browser-use, etc.) are fine if they use the proxy base URLs
  and the provided CDP browsers (never launch their own Chromium in scored runs; connect via CDP).
- Suggested shape: an orchestrator (model) that reads the brief and spawns one worker per site / per position
  (tender, spec, ERP, mail run in parallel on separate browsers), workers return structured evidence with source + timestamps,
  a calculator step (plain code, deterministic rounding rules) computes prices, a verifier agent cross-checks authority/dates/
  tender reference/superseded records, then one agent fills the configurator and signs with the TAN.
- Models: hosted `qwen` (32 concurrent, 256k ctx, vision) for navigation/extraction; `clef` (16 concurrent) for quick
  choice/score decisions; use a stronger own-key model only for hard reasoning (cost x time only matters as tie-break after quality,
  so quality first). Every call counts simulated cost, so keep prompts short and use cached prefixes. Warm-up can take minutes: long timeouts.
  List hosted models in `agent.json` `"models"` (`[]` skips warm-up wait).
- All LLM traffic via `$LLM_PROXY_URL` / `<MODEL>_BASE_URL`; send your own User-Agent on platform HTTP calls (default Python UA gets 403).

### Run discipline
- Clock starts at the `run` command; scored 20 min; submit the best answer by ~1 min before end (`run.seconds_left`); late/no answer = 0.
- Build a fallback: keep a best-so-far answer and submit it on a timer even if the swarm is unfinished.
- Submission 400 with `issues` is not final (fix + resend); 202 is final and the process has 10 s to exit.
- Sessions time out (ERP, portals): handle re-login. Prefer partial-correct signed bid over nothing.
- Test runs (2 h, unscored) on public data first; use `atira` MCP (`diagnose_run`, `run_logs`, `run_llm_calls`, `run_profile`) to debug.
  Never copy values from logs/screenshots/answers into the package.

### Links
Challenge: https://ehl-challenge.atira.ai/challenges/rfq · fair play: /docs/fair-play.md · run contract: /docs/run-contract.md ·
API: /docs/api.md · CLI: /docs/cli.md · llms.txt: /llms.txt · leaderboard: /challenges/rfq/leaderboard.md
