"""Quick decisions with the hosted Clef model: cheap, fast, calibrated. Use it for the many small judgements
(what kind of page is this, which of these links, is this the right document) and keep Qwen for reading and
reasoning. Each call still adds its simulated cost, but at a fraction of a chat call.

    kind, p = await decide.choose({"page": text}, "What is this page?", PAGE_KINDS)
    p_yes   = await decide.yes({"mail": subject}, "Is this mail about tender REF exactly?")

The API (POST $CLEF_BASE_URL/v1/decide) takes a `state` (text or JSON), up to 4 images as base64 data URLs and
named questions: "choice" (criteria: {option: description}), "noul" (probability of yes) and "score"
(criteria: a list of 2-10 levels, lowest first). Set "clef" in agent.json "models" so a run waits for it.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx

from swarm.bidmath import plain

log = logging.getLogger("decide")

MAX_IMAGES = 4
_sem = asyncio.Semaphore(12)        # the team may run 16 Clef calls at once, across all its runs
usage = {"calls": 0, "input_tokens": 0}
_transport = None                   # tests swap in an httpx.MockTransport

# what a page can be, for agents that need to know where they are before acting
PAGE_KINDS = {
    "login": "a sign-in form asking for username and password",
    "consent": "a cookie or consent wall that must be accepted before the content",
    "error": "an error or 'service unavailable' page that offers a retry",
    "locked": "content blocked behind a robot check, slider or other unlock step",
    "list": "a list, table or search results to pick an entry from",
    "document": "a document, record, message or form with the actual content",
    "other": "anything else",
}


class ClefError(RuntimeError):
    pass


def data_url(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(png).decode()}"


async def decide(state, questions: dict, images: list[bytes] | None = None, retries: int = 2) -> dict:
    """One Clef call with several named questions; returns the `answers` object."""
    body = {"state": state, "questions": questions}
    if images:
        body["images"] = [data_url(i) if isinstance(i, (bytes, bytearray)) else i for i in images[:MAX_IMAGES]]
    url = os.environ["CLEF_BASE_URL"].rstrip("/") + "/v1/decide"
    headers = {"Authorization": f"Bearer {os.environ['CLEF_API_KEY']}"}
    last = None
    for attempt in range(retries + 1):
        try:
            async with _sem, httpx.AsyncClient(timeout=180, transport=_transport) as client:
                response = await client.post(url, json=body, headers=headers)
            if response.status_code == 400:      # our question is malformed: retrying will not help
                raise ClefError(response.text[:300])
            response.raise_for_status()
            reply = response.json()
            usage["calls"] += 1
            usage["input_tokens"] += (reply.get("usage") or {}).get("input_tokens", 0)
            return reply["answers"]
        except ClefError:
            raise
        except Exception as error:  # noqa: BLE001 - timeouts, 5xx, a model waking up
            last = error
            await asyncio.sleep(2 * (attempt + 1))
    raise ClefError(f"Clef unavailable: {type(last).__name__}: {last}")


async def choose(state, instructions: str, options: dict[str, str],
                 images: list[bytes] | None = None) -> tuple[str, float]:
    """Pick one of `options` ({key: description}); returns (key, confidence)."""
    answers = await decide(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}},
                           images)
    q = answers["q"]
    return q["choice"], float(q.get("confidence", q.get("probabilities", {}).get(q["choice"], 0)))


async def yes(state, question: str, images: list[bytes] | None = None) -> float:
    """Probability (0-1) that the answer to a yes/no question about `state` is yes."""
    answers = await decide(state, {"q": {"type": "noul", "instructions": question}}, images)
    return float(answers["q"]["noul"])


async def score(state, instructions: str, levels: list[str], images: list[bytes] | None = None) -> float:
    """A rating on `levels` (lowest first), as a float index into them."""
    answers = await decide(state, {"q": {"type": "score", "instructions": instructions, "criteria": levels}},
                           images)
    return float(answers["q"]["score"])


async def page_kind(page, with_screenshot: bool = False, chars: int = 2500) -> tuple[str, float]:
    """What kind of page a Playwright page shows (see PAGE_KINDS), from its text and optionally a screenshot."""
    text = plain(await page.evaluate("() => (document.querySelector('main') || document.body).innerText"))[:chars]
    shots = [await page.screenshot(type="jpeg", quality=60)] if with_screenshot else None
    state = {"url": page.url, "title": plain(await page.title()), "text": text}
    return await choose(state, "What kind of page is the browser showing?", PAGE_KINDS,
                        [data_url(s, "image/jpeg") for s in shots] if shots else None)
