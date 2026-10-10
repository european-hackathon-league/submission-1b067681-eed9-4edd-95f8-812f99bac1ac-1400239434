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

## Working alongside the rest of the team (read docs/COLLABORATION.md)

- `main` is the shared baseline; other developers and their agents work on other branches at the same time.
- **At the start of every task** run `uv run --no-sync python scripts/team.py status --json` and read
  `docs/INTEGRATION.md`. Do not build what already exists in `main` or on another branch; extend it.
- If your branch is behind `main`, run `uv run --no-sync python scripts/team.py sync` (safe: changes nothing unless the
  merge is clean and the tests pass) before editing files that `main` also changed.
- Before changing a file that `status` lists as changed on another branch, say so to the user: it is a likely conflict.
- When two parts must agree (bid rules, result shapes, number formats), extend `tests/test_contracts.py`.
- One task per worktree: `git worktree add ../<task> -b <user>/<task> origin/main`. Never edit another agent's worktree.
- Never commit `.integration/` or `.swarm/`; never force-push; never push to `main` (the integrator moves it).

