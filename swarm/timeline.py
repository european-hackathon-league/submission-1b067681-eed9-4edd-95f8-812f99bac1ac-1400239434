"""Where a run's time goes: spans (useful work, waits, recovery, model queue, extraction) and milestones.

The platform keeps only the tail of a run's log, so the timeline logs compact lines: a progress line per minute during
collection, one line per milestone, and a short report at the end (category totals, slowest spans, repeated work, time
to first usable bid / complete positions / accepted submission). Locally the full span list is written to `.swarm/`.

    with timeline.span("wait.dep", "requirements"):      # who: the running agent (context variable)
        ...
    timeline.mark("first usable bid", positions=2)       # milestone, first occurrence only
    timeline.count("reads.repeat")                        # repeated work

Categories:
  browse.step     one browsing step (model reply + action + page state): the useful browser work
  model.queue     waiting for a free model slot (our concurrency limit)
  model.call      model latency (analysis, planning, guard; browsing steps contain their own call)
  extract         saved page -> facts (model extraction)
  wait.dep        an agent waiting for an upstream result
  recovery        browser checks, hung tabs, reattaching, retries after failures
  guard           popup sweeps before a step
  browse.failed   whole browses that ended without success (repeated later)
"""
import contextlib
import contextvars
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("timeline")

AGENT: contextvars.ContextVar[str] = contextvars.ContextVar("swarm_agent", default="-")


def who() -> str:
    return AGENT.get()


class Timeline:
    def __init__(self, start: float | None = None):
        self.start = time.monotonic() if start is None else start
        self.spans: list[tuple[str, str, str, float, float]] = []   # category, agent, label, start, end (run clock)
        self.milestones: dict[str, tuple[float, dict]] = {}
        self.counters: dict[str, int] = defaultdict(int)
        self.max_spans = 20_000

    def now(self) -> float:
        return time.monotonic() - self.start

    def add(self, category: str, label: str = "", start: float | None = None, end: float | None = None,
            agent: str | None = None) -> None:
        end = self.now() if end is None else end
        if len(self.spans) < self.max_spans:
            self.spans.append((category, agent or who(), str(label)[:80], end if start is None else start, end))

    @contextlib.contextmanager
    def span(self, category: str, label: str = "", agent: str | None = None):
        start = self.now()
        try:
            yield
        finally:
            self.add(category, label, start, self.now(), agent)

    def mark(self, name: str, **info) -> bool:
        """Record a milestone once (its first time); True if this call recorded it."""
        if name in self.milestones:
            return False
        self.milestones[name] = (self.now(), info)
        log.info("milestone %s at %s %s", name, clock(self.now()), json.dumps(info, default=str)[:200] if info else "")
        return True

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    # ---- reports --------------------------------------------------------------------------------------------------
    def totals(self, since: float = 0.0) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for cat, _, _, s, e in self.spans:
            if e < since:
                continue
            t = out.setdefault(cat, {"s": 0.0, "n": 0})
            t["s"] += e - max(s, since)
            t["n"] += 1
        return {k: {"s": round(v["s"], 1), "n": v["n"]} for k, v in sorted(out.items(), key=lambda kv: -kv[1]["s"])}

    def by_agent(self, category: str) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for cat, agent, _, s, e in self.spans:
            if cat == category:
                out[agent] += e - s
        return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}

    def busy(self, category: str, agent: str | None = None) -> float:
        """Wall-clock seconds covered by at least one span of `category` (overlaps counted once)."""
        intervals = sorted((s, e) for cat, a, _, s, e in self.spans if cat == category and (agent is None or a == agent))
        total, cur_s, cur_e = 0.0, None, None
        for s, e in intervals:
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    total += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        if cur_e is not None:
            total += cur_e - cur_s
        return round(total, 1)

    def median_step(self, last: int = 40, default: float = 14.0) -> float:
        """Median duration of the latest browsing steps: the run's measured pace (model + browser)."""
        steps = [e - s for cat, _, _, s, e in self.spans if cat == "browse.step"][-last:]
        if len(steps) < 5:
            return default
        steps.sort()
        return steps[len(steps) // 2]

    def slowest(self, n: int = 8, categories=None) -> list[tuple]:
        spans = [x for x in self.spans if categories is None or x[0] in categories]
        return [(cat, agent, label, round(e - s, 1), clock(s)) for cat, agent, label, s, e in
                sorted(spans, key=lambda x: x[3] - x[4])[:n]]

    def report(self) -> dict:
        m = {k: round(v[0], 1) for k, v in sorted(self.milestones.items(), key=lambda kv: kv[1][0])}
        return {"elapsed": round(self.now(), 1), "milestones": m, "totals": self.totals(),
                "wall": {c: self.busy(c) for c in ("browse.step", "model.queue", "extract", "wait.dep", "recovery")},
                "steps_by_agent": self.by_agent("browse.step"), "waits_by_agent": self.by_agent("wait.dep"),
                "recovery_by_agent": self.by_agent("recovery"), "counters": dict(self.counters),
                "slowest": self.slowest(8, ("browse.step", "model.call", "extract", "wait.dep", "recovery",
                                           "model.queue", "browse.failed"))}

    def log_report(self) -> None:
        """A few compact lines (the platform keeps only the log's tail)."""
        r = self.report()
        log.info("TIMELINE milestones %s", json.dumps(r["milestones"]))
        log.info("TIMELINE totals %s wall %s", json.dumps(r["totals"]), json.dumps(r["wall"]))
        log.info("TIMELINE agents steps %s waits %s recovery %s", json.dumps(r["steps_by_agent"]),
                 json.dumps(r["waits_by_agent"]), json.dumps(r["recovery_by_agent"]))
        log.info("TIMELINE counters %s", json.dumps(r["counters"]))
        log.info("TIMELINE slowest %s", json.dumps(r["slowest"], default=str))

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"report": self.report(), "spans": self.spans,
                                    "milestones": {k: [round(t, 2), i] for k, (t, i) in self.milestones.items()}},
                                   default=str), encoding="utf-8")


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


_CURRENT = Timeline()


def current() -> Timeline:
    return _CURRENT


def reset(start: float | None = None) -> Timeline:
    global _CURRENT
    _CURRENT = Timeline(start)
    return _CURRENT


def span(category: str, label: str = "", agent: str | None = None):
    return _CURRENT.span(category, label, agent)


def add(category: str, label: str = "", start: float | None = None, end: float | None = None,
        agent: str | None = None) -> None:
    _CURRENT.add(category, label, start, end, agent)


def mark(name: str, **info) -> bool:
    return _CURRENT.mark(name, **info)


def count(name: str, n: int = 1) -> None:
    _CURRENT.count(name, n)


def now() -> float:
    return _CURRENT.now()
