# Notes for AI coding agents

- This repo is an Atira EHL challenge agent (starter kit). Python 3.12 + uv, Playwright, hosted Qwen via OpenAI SDK.
- Your agent goes in `solve(task, page)` in `agent.py`. `run.py`, `browsers.py`, `models.py` are platform plumbing.
- `agent.json` defines how the sandbox installs/runs (`uv sync`, `uv run --no-sync python agent.py`) and which hosted models it uses.
- A model must choose navigation/decisions at runtime. No hardcoded answers, fixed scripts or crawlers (submissions are reviewed).
- Scored run is 20 min: submit the best answer ~1 minute before the end (`run.seconds_left(task)`).
- Local run: terminal 1 `atira models proxy`; terminal 2 `atira scout --env | Out-String | Invoke-Expression` then `uv run --no-sync python agent.py`.
- Test in the real sandbox: `atira test-run . --follow`. Ranked submit: `atira submit . --follow` (only when the team agrees).
- The `atira` MCP server is available for debugging runs (logs, model calls, timing).
- Only inspect, save, or reference run data for runs the user identifies as theirs or runs launched for them with a recorded launch receipt. Historical indexes and visible team runs are not proof of ownership. Never collect other teams' run data.
- Never read, print or commit `.env` or API keys.
- This repo uses Entire: commit your work with normal `git commit` / `git push` so session
  checkpoints get linked. Do not push `entire/*` shadow branches manually.
