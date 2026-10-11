"""The blackboard: the one place agents share data. Evidence goes in with its source; results are named values.

Everything is JSON-serialisable so a board can be dumped to `.swarm/` and replayed in local development.
Single event loop, so no locks; `wait_for` lets a downstream agent start as soon as upstream posts.
"""
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Evidence:
    kind: str            # label agreed between producer and consumer, e.g. "tender.position" (see docs/SWARM.md)
    site: str            # siteId the fact came from
    data: dict           # the extracted facts, as found (no interpretation)
    source: str = ""     # URL / document / mail subject: where exactly it was read
    author: str = ""     # agent name
    note: str = ""       # caveats: superseded? withdrawn? timezone? uncertain?
    ts: float = field(default_factory=time.time)


class Blackboard:
    def __init__(self):
        self.evidence: list[Evidence] = []
        self.results: dict = {}      # named outputs: "quantities", "materials", "prices", "bid", "answer", ...
        self.errors: list[dict] = []
        self._changed = asyncio.Condition()

    async def post(self, item: Evidence) -> None:
        async with self._changed:
            self.evidence.append(item)
            self._changed.notify_all()

    def query(self, kind: str | None = None, site: str | None = None, prefix: bool = False) -> list[Evidence]:
        def match(e: Evidence) -> bool:
            if kind is not None and not (e.kind.startswith(kind) if prefix else e.kind == kind):
                return False
            return site is None or e.site == site
        return [e for e in self.evidence if match(e)]

    async def set(self, key: str, value) -> None:
        async with self._changed:
            self.results[key] = value
            self._changed.notify_all()

    def get(self, key: str, default=None):
        return self.results.get(key, default)

    async def wait_for(self, kind: str | None = None, key: str | None = None, timeout: float = 600):
        """Block until evidence of `kind` exists (returns the list) or result `key` is set (returns it)."""
        def ready():
            if key is not None and key in self.results:
                return self.results[key]
            if kind is not None and self.query(kind):
                return self.query(kind)
            return None
        if ready() is not None:
            return ready()
        from swarm import timeline
        with timeline.span("wait.dep", key or kind or ""):
            async with self._changed:
                await asyncio.wait_for(self._changed.wait_for(lambda: ready() is not None), timeout)
        return ready()

    def error(self, agent: str, message: str) -> None:
        self.errors.append({"agent": agent, "error": message, "ts": time.time()})

    def to_json(self) -> dict:
        return {"evidence": [asdict(e) for e in self.evidence], "results": self.results, "errors": self.errors}

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Blackboard":
        raw = json.loads(path.read_text(encoding="utf-8"))
        board = cls()
        board.evidence = [Evidence(**e) for e in raw.get("evidence", [])]
        board.results = raw.get("results", {})
        board.errors = raw.get("errors", [])
        return board
