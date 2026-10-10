"""Helpers for analysts: read the pages the scrapers saved and let the model turn them into structured JSON.

The scrapers decide which pages matter (`read_page` with a topic -> Evidence kind "<role>.page"); analysts send that
text plus a JSON shape to the model and validate the reply in code. Long texts are split at page boundaries.
"""
import asyncio
import logging
import re

log = logging.getLogger("extract")

SYSTEM = ("Extract facts from German business web pages for bid calculation. Copy values exactly as shown (keep "
          "number strings with locale marker like '(en-US)', keep units). Never invent; value not on page -> null. "
          "Reply JSON only, in requested shape.")
CHUNK = 40_000


def pages(ctx, role: str) -> list:
    """Saved page evidence of a role ('tender', 'spec', 'erp', 'mail'), oldest first, exact duplicates dropped."""
    seen, out = set(), []
    for e in ctx.board.query(f"{role}.page"):
        key = (e.data.get("url"), hash(e.data.get("text", "")))
        if key not in seen:
            seen.add(key)
            out.append(e)
    from swarm.blackboard import Evidence
    for e in ctx.board.query(role + ".", prefix=True):
        if not e.kind.endswith(".page") and e.data.get("_observed_text"):
            key = (e.source, hash(e.data["_observed_text"]))
            if key not in seen:
                seen.add(key)
                out.append(Evidence(role + ".page", e.site, {"topic": e.kind, "url": e.source,
                                  "text": e.data["_observed_text"]}, source=e.source, author=e.author))
    return out


def page_block(e) -> str:
    d = e.data
    return f"=== PAGE topic={d.get('topic')!r} url={d.get('url')} title={d.get('title')!r}\n{d.get('text', '')}"


def records(ctx, kind_prefix: str) -> list[dict]:
    """Facts the browsing model recorded directly with `record` (kind starting with the prefix)."""
    return [{"kind": e.kind, **(e.data or {}), "_source": e.source, "_note": e.note}
            for e in ctx.board.query(kind_prefix, prefix=True) if not e.kind.endswith(".page")]


PAGE_BREAK = "\n\n----- next page -----\n\n"


def chunks(blocks: list[str], size: int = CHUNK) -> list[str]:
    """Pack blocks into chunks of about `size` characters; a long block is split at its page breaks first (so a
    paged list is cut between pages, not inside an entry), and only then at `size`."""
    out, cur = [], ""
    for b in blocks:
        head = b.split("\n", 1)[0] + "\n" if b.startswith("=== PAGE") else ""
        parts = b.split(PAGE_BREAK)
        pieces = []
        for i, part in enumerate(parts):
            part = part if i == 0 else head + part
            pieces += [part[j:j + size] for j in range(0, len(part), size)] or [""]
        for piece in pieces:
            if cur and len(cur) + len(piece) > size:
                out.append(cur)
                cur = ""
            cur += piece + "\n\n"
    if cur.strip():
        out.append(cur)
    return out


async def extract(ctx, instruction: str, shape: str, blocks: list[str], *, list_key: str | tuple | None = None,
                  context: str = "", chunk_size: int = CHUNK) -> dict:
    """Ask the model to extract `shape` from the blocks. With `list_key` (one key or several), chunks are processed in
    parallel and the lists under those keys are concatenated (for long paged lists); otherwise one call."""
    keys = (list_key,) if isinstance(list_key, str) else tuple(list_key or ())
    if not blocks:
        return {k: [] for k in keys}
    user_head = f"{instruction}\n\nJSON shape:\n{shape}\n" + (f"\nContext:\n{context}\n" if context else "")
    if not keys:
        out = await ctx.llm.ask_json(SYSTEM, user_head + "\nPAGES:\n" + "\n\n".join(blocks))
        return out if isinstance(out, dict) else {}
    parts = chunks(blocks, chunk_size)
    replies = await asyncio.gather(*(ctx.llm.ask_json(SYSTEM, user_head + "\nPAGES:\n" + p) for p in parts),
                                   return_exceptions=True)
    merged: dict = {k: [] for k in keys}
    for r in replies:
        if isinstance(r, Exception):
            merged["_incomplete"] = True
            log.warning("extract chunk failed: %s", type(r).__name__)
            continue
        if isinstance(r, list) and len(keys) == 1:
            merged[keys[0]] += r
        elif isinstance(r, dict):
            for k, v in r.items():
                if k in keys:
                    merged[k] += v if isinstance(v, list) else []
                elif v not in (None, "", [], {}):
                    merged.setdefault(k, v)
    return merged


def brief_section(brief: str, *names: str) -> str:
    """The text of markdown sections whose heading contains one of `names` (case-insensitive)."""
    out, keep = [], False
    for line in brief.splitlines():
        if line.startswith("#"):
            keep = any(n.lower() in line.lower() for n in names)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def brief_intro(brief: str, limit: int = 1500) -> str:
    """The brief up to its first second-level section: who we are and what the tender is."""
    head = re.split(r"\n##\s", brief, maxsplit=1)[0]
    return head.strip()[:limit]
