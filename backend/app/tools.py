"""Example tools for the agent. Copy one of these to add your own.

The SDK builds the tool schema from the type hints and the docstring,
so keep both accurate - the model reads them to decide when to call a tool.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agents.decorators import tool

DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()
MAX_CHARS = 8_000  # keep tool outputs small so they don't blow up the context


@tool
async def fetch_url(url: str) -> str:
    """Fetch a web page or JSON endpoint over HTTP(S) and return its text (truncated).

    Args:
        url: Full URL starting with http:// or https://.
    """
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "ehl-agent/0.1"})
    text = resp.text[:MAX_CHARS]
    return f"HTTP {resp.status_code}\n{text}"


@tool
def read_local_file(filename: str) -> str:
    """Read a text file from the backend/data folder.

    Args:
        filename: File name relative to backend/data, e.g. "sample.txt".
    """
    path = (DATA_DIR / filename).resolve()
    if not path.is_relative_to(DATA_DIR):
        return "Error: only files inside backend/data can be read"
    if not path.is_file():
        available = sorted(p.name for p in DATA_DIR.iterdir() if p.is_file())
        return f"Error: {filename} not found. Available files: {available}"
    return path.read_text(errors="replace")[:MAX_CHARS]


@tool
def call_external_api(city: str) -> str:
    """Get the current weather for a city from an external weather service.

    This is a dummy stand-in: replace the body with a real API call
    (the challenge partner's API, a database, etc.).

    Args:
        city: City name, e.g. "Munich".
    """
    fake = {
        "city": city,
        "temperature_c": round(random.uniform(5, 18), 1),
        "condition": random.choice(["sunny", "cloudy", "rain", "fog"]),
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "dummy-weather-api",
    }
    return json.dumps(fake)


ALL_TOOLS = [fetch_url, read_local_file, call_external_api]
