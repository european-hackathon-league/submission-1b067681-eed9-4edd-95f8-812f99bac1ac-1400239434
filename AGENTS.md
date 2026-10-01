# Notes for AI coding agents

- Backend: Python 3.11+, FastAPI, OpenAI Agents SDK (`openai-agents`, `from agents.decorators import tool`).
  Venv at `backend/.venv`; run with `make run` (port 8000).
- Agent lives in `backend/app/agent.py`, tools in `backend/app/tools.py` (add to `ALL_TOOLS`).
- Frontend is plain HTML/JS in `frontend/`, served by FastAPI at `/`.
- Never read, print or commit `.env` or API keys. Only `.env.example` is committed.
- This repo uses Entire: commit your work with normal `git commit` / `git push` so session
  checkpoints get linked. Do not push `entire/*` shadow branches manually.
