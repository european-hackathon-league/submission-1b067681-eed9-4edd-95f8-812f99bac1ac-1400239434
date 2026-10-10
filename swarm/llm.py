"""Model access for agents: concurrency-limited, usage-counted, JSON-friendly. Qwen by default (hosted, free)."""
import asyncio
import json
import re
import os
import httpx

CALL_TIMEOUT_S = float(os.environ.get("SWARM_MODEL_TIMEOUT_S", "55"))
HTTP_TIMEOUT = httpx.Timeout(CALL_TIMEOUT_S, connect=8, write=15, pool=8)

import models
from openai import APIConnectionError, APIStatusError
from swarm import timeline

# Model slots (team limit: 32 concurrent Qwen calls). Browsing steps take a slot of the total; analysis, planning and
# guard calls also take one of the analysis slots, so extraction can never starve the browsing agents.
MAX_CONCURRENT = int(os.environ.get("SWARM_LLM_CONCURRENCY", "16"))
ANALYSIS_CONCURRENT = int(os.environ.get("SWARM_ANALYSIS_CONCURRENCY", "0")) or max(1, MAX_CONCURRENT // 2)


def parse_json(text: str):
    """The first JSON object/array in a model reply (tolerates ``` fences and chatter)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise ValueError(f"no JSON in model reply: {text[:200]}")


class LLM:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT, analysis_concurrent: int | None = None):
        self._sem = asyncio.Semaphore(max_concurrent)   # team limit is 32 concurrent Qwen calls
        self._analysis_sem = asyncio.Semaphore(analysis_concurrent or
                                               (ANALYSIS_CONCURRENT if max_concurrent == MAX_CONCURRENT
                                                else max(1, max_concurrent // 2)))
        self._client = None
        self.calls = 0
        self.tokens = {"prompt": 0, "completion": 0}

    @property
    def client(self):
        self._client = self._client or models.qwen().with_options(timeout=HTTP_TIMEOUT, max_retries=0)
        return self._client

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, model: str = models.QWEN_MODEL,
                   think: bool = False, **kwargs):
        """One chat completion; returns the message (`.content`, `.tool_calls`). `think=True` lets Qwen run its
        separate reasoning pass (slower, more tokens): use it for judgement calls, not for plain extraction."""
        args = dict(model=model, messages=messages, **kwargs)
        args.setdefault("temperature", 0)
        if not think and model == models.QWEN_MODEL:
            args["extra_body"] = {**(args.get("extra_body") or {}), **models.NO_THINKING}
        if tools:
            args["tools"] = tools
        queued = timeline.now()
        async with self._analysis_sem, self._sem:
            timeline.add("model.queue", "analysis", queued)
            # Bound each attempt, including providers that leave a connection open without replying.
            for attempt in range(2):
                started = timeline.now()
                try:
                    async with asyncio.timeout(CALL_TIMEOUT_S):
                        reply = await self.client.chat.completions.create(**args)
                    timeline.add("model.call", "think" if think else "chat", started)
                    break
                except (TimeoutError, models.httpx.TransportError, APIConnectionError) as error:
                    timeline.add("recovery", f"model {type(error).__name__}", started)
                    timeline.count("model.failed")
                    if attempt:
                        raise
                    await asyncio.sleep(0.5)
                except APIStatusError as error:
                    timeline.add("recovery", f"model HTTP {error.status_code}", started)
                    timeline.count("model.failed")
                    if attempt or (error.status_code < 500 and error.status_code != 429):
                        raise
                    await asyncio.sleep(0.5)
        self.calls += 1
        if reply.usage:
            self.tokens["prompt"] += reply.usage.prompt_tokens or 0
            self.tokens["completion"] += reply.usage.completion_tokens or 0
        return reply.choices[0].message

    async def ask(self, system: str, user: str, **kwargs) -> str:
        message = await self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)
        return message.content or ""

    async def ask_json(self, system: str, user: str, retries: int = 1, **kwargs):
        """Ask for JSON; re-ask once with the parse error if the reply is not valid."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs.setdefault("response_format", {"type": "json_object"})    # Qwen then always answers with JSON
        for _ in range(retries + 1):
            reply = (await self.chat(messages, **kwargs)).content or ""
            try:
                return parse_json(reply)
            except ValueError as error:
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"Invalid: {error}. Reply with valid JSON only."}]
        raise ValueError("model did not return valid JSON")
