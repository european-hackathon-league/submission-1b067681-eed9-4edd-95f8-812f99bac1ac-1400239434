"""FastAPI app: POST /api/chat runs the agent; / serves the frontend."""

import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # must run before the agent/openai client is created

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from openai import APIStatusError, AuthenticationError  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import run_agent  # noqa: E402

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="EHL Finale Agent")
app.add_middleware(  # lets a separately served frontend (e.g. Vite on :5173) call us
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatTurn] = []


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[str]


@app.get("/api/health")
def health():
    return {"ok": True, "openai_key_set": bool(os.getenv("OPENAI_API_KEY"))}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(500, "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key.")
    try:
        return await run_agent(req.message, [t.model_dump() for t in req.history])
    except AuthenticationError:
        raise HTTPException(401, "OpenAI rejected the API key (check OPENAI_API_KEY in .env).")
    except APIStatusError as e:
        raise HTTPException(502, f"OpenAI API error {e.status_code}: {e.message}")
    except Exception as e:
        log.exception("agent run failed")
        raise HTTPException(500, f"Agent error: {type(e).__name__}: {e}")


# Serve the frontend at / (keep this last so /api routes win)
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
