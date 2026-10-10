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
