# EHL Finale Template

Starter for the EHL Grand Finale (TUM.ai, 10–11 Oct 2026, agentic AI):
FastAPI backend + OpenAI Agents SDK agent with example tools + a minimal chat UI.
**Entire is set up in this repo — submissions without Entire are not accepted.**
Entire status: enabled and verified (checkpoint pushed on first agent commit).

## Run locally (< 2 min)

Requirements: macOS/Linux, Python 3.11+, git. (Node not needed.)

> **macOS: don't clone into `~/Desktop` or `~/Documents` if they sync to iCloud.**
> iCloud evicts files it thinks are unused (including `.venv` and git hooks), and then
> imports/commits hang while it re-downloads them. Use e.g. `~/Developer` instead.

```bash
mkdir -p ~/Developer && cd ~/Developer
git clone https://github.com/ksiox1337/ehl-finale-template.git
cd ehl-finale-template
make setup            # venv + deps + creates .env from .env.example
# edit .env -> OPENAI_API_KEY=sk-...
make run              # http://localhost:8000
```

Without `make`:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env   # add your key
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
```

Test from the terminal:

```bash
curl -s localhost:8000/api/chat -H 'content-type: application/json' \
  -d '{"message":"What is the weather in Munich? Also read sample.txt"}'
```

Errors from `/api/chat`: **401** = wrong key in `.env`; **429 insufficient_quota** = the
OpenAI account has no credits (add billing or use the hackathon credits) — not a code bug.

## Entire setup (every teammate, once per machine)

Official steps (from the EHL Notion + https://github.com/entireio/cli):

```bash
curl -fsSL https://entire.io/install.sh | bash   # installs to ~/.local/bin
entire version                                   # check it's on PATH
cd ehl-finale-template
entire enable --agent claude-code                # or: cursor, codex, copilot-cli, opencode, ...
entire status
```

`.entire/settings.json` and the Claude Code hooks in `.claude/settings.json` are
already committed, so on a fresh clone `entire enable` mainly installs the **git hooks**
(they live in `.git/hooks`, which is never cloned) — **you must run it on every clone.**

**Important:** a checkpoint is only created when you commit **while an AI agent session
is active in this repo** (Claude Code, Cursor, Codex, … with Entire hooks installed).
Let the agent make the commit (or commit while the session is open).
A correct push prints:

```
[entire] Pushing 1 checkpoint ref(s) to origin... done
```

If you commit by hand with no agent session, no checkpoint is created — that's expected,
just make sure most of the work goes through an agent. Troubleshooting:
`entire status`, `entire doctor`, `entire enable --force`.
If a push prints the `[entire] ... done` line but then `remote: fatal error in commit_refs`
/ `[remote rejected] main`, just run `git push` again (seen once on GitHub; the retry worked).
Using a different agent? `entire agent add cursor` (etc.), commit the generated hook file.

## Project structure

```
backend/
  app/main.py       FastAPI: POST /api/chat, GET /api/health, serves frontend/ at /
  app/agent.py      Agent definition (instructions, tools, model) + run_agent()
  app/tools.py      Example tools: fetch_url, read_local_file, call_external_api (dummy)
  data/             Files the agent may read (read_local_file is sandboxed to this folder)
  requirements.txt
frontend/           Plain HTML/JS chat page (no build step)
.entire/            Entire settings (committed)
.claude/            Claude Code hooks installed by Entire (committed)
.env.example        Copy to .env — never commit .env
```

**Add a tool:** write a function with type hints + docstring in `tools.py`, decorate with
`@tool`, add it to `ALL_TOOLS`. **Change model:** `OPENAI_MODEL=...` in `.env`.
**API:** `POST /api/chat {"message": str, "history": [{"role","content"}]}` →
`{"reply": str, "tool_calls": [str]}`.

## How we work

**[PLAYBOOK.md](PLAYBOOK.md)** (in Russian): our method for the weekend — Design Sprint × TRIZ,
timeline, agent patterns, demo/pitch, submission checklist and fill-in templates.

## Who does what (3 people)

| Person | Owns | Sat goal |
|---|---|---|
| **A — Agent/Backend** | `agent.py`, `tools.py`, challenge APIs/data, prompt design | Real tools for the chosen challenge working via `/api/chat` |
| **B — Frontend/Demo** | `frontend/` (or swap to React/Vite), demo flow, UX | Demo-able UI by Sat evening |
| **C — Product/Pitch + integration** | Challenge fit, data in `backend/data/`, eval examples, 6-min pitch, submission on ehl.gg | Pitch outline Sat, rehearsal Sun 10:00 |

Rules: work on short-lived branches or small commits to `main`, pull often,
**commit through your agent so Entire records checkpoints**, never commit `.env`.

## Key times

- Sat 11:00–11:30 Entire onboarding · **Sat 13:00 challenge selection deadline** (captain on ehl.gg)
- **Sun 12:00 submission deadline** · pitches: strict 6 min incl. Q&A
- OpenAI credits: $75 per person · Questions: Discord https://discord.gg/4UDNd5TAg

## Links

[ehl.gg](https://ehl.gg) · [Rules](https://ehl.gg/rules) · [Finale page](https://ehl.gg/matches/finale) ·
[All-you-need-to-know (Notion)](https://tum-ai.notion.site/EHL-Finale-All-you-need-to-know-3eb7306bfd62808993e9ec18c2d64834) ·
[Entire](https://entire.io) · [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) ·
makeathon@tum-ai.com
