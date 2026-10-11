"""Turn a saved run log (.swarm/runs/<run>-log.md) into a timeline: where the time went, per agent.

Works on logs of any version: it reads the swarm's own lines (phases, browse steps, analyst results, submission) and,
when present, the runtime timeline's `milestone` / `progress` / `TIMELINE` lines (swarm/timeline.py).

    uv run --no-sync python scripts/timeline.py .swarm/runs/run_xxx-log.md [--gap 30]

Reports (run clock = seconds since the COLLECT phase started):
- milestones: brief, site roles, first page per site, first requirements, first ERP candidates, materials matched,
  each phase, submission attempts, accepted submission (plus the runtime's own milestones)
- per agent: steps, first/last step, busy time, idle gaps longer than --gap seconds (waiting or stalled)
- trouble: state timeouts, empty model replies, crashed browses, cleanup overruns, model timeouts, failed extractions
The log holds scenario values: read the output, never copy values from it into the package.
"""
import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) (\S+) (.*)$")
STEP = re.compile(r"^(\S+) step (\d+): (.*)$")
TROUBLE = {"state timeouts": "repeated browser-state timeouts", "no model reply": "step \\d+: no reply",
           "crashed browses": "browser agent failed|swarm \\w+ failed", "cleanup overruns": "cleanup exceeded",
           "model timeouts": "LLM call timed out|model TimeoutError", "failed extractions": "extract chunk failed",
           "hung tabs closed": "closed a hung tab", "browser moved": "is unreachable"}
MILESTONES = [("brief read", r"BriefReader brief facts"), ("site roles", r"sites site roles"),
              ("first requirements", r"RequirementAnalyzer current revision \S+, requirements \[['\"]"),
              ("positions known", r"QuantityAnalyzer [1-9]\d* positions"),
              ("first ERP candidates", r"search \S+: candidates \[['\"]"),
              ("first draft bid", r"BidDrafter draft: [1-9]"), ("materials matched", r"MaterialMatcher materials:"),
              ("collect end", r"phase VERIFY"), ("submit start", r"phase SUBMIT"),
              ("submission attempt", r"submission attempt \d+ finished"),
              ("accepted", r"portal acceptance confirmed|milestone accepted submission")]


def parse(path: Path):
    events = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(raw.strip())
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000
            events.append((ts, m.group(3), m.group(4)))
    events.sort(key=lambda e: e[0])
    return events


def clock(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"


def analyse(events, gap: float) -> str:
    if not events:
        return "no timestamped lines"
    start = next((ts for ts, logger, msg in events if logger == "swarm" and "phase COLLECT" in msg), events[0][0])
    out = [f"run clock 00:00 = COLLECT start; log covers {clock(events[0][0] - start)}..{clock(events[-1][0] - start)} "
           "(the platform keeps only the tail of long logs: early lines may be missing)", "", "## Milestones"]
    seen = set()
    for ts, logger, msg in events:
        text = f"{logger} {msg}"
        for name, pattern in MILESTONES:
            if (name not in seen or name == "submission attempt") and re.search(pattern, text):
                seen.add(name)
                out.append(f"- {clock(ts - start)} {name}: {msg[:110]}")
        if logger == "timeline" and msg.startswith("milestone"):
            out.append(f"- (runtime) {msg[:140]}")
    steps = defaultdict(list)
    for ts, logger, msg in events:
        m = STEP.match(msg) if logger == "browse" else None
        if m:
            steps[m.group(1)].append(ts)
    out += ["", "## Browsing per agent", "| agent | steps | first | last | median step | idle gaps > %ds |" % gap,
            "| --- | --- | --- | --- | --- | --- |"]
    for agent, times in sorted(steps.items(), key=lambda kv: kv[1][0]):
        deltas = [b - a for a, b in zip(times, times[1:])]
        med = sorted(deltas)[len(deltas) // 2] if deltas else 0
        gaps = [f"{clock(a - start)} +{b - a:.0f}s" for a, b in zip(times, times[1:]) if b - a > gap]
        out.append(f"| {agent} | {len(times)} | {clock(times[0] - start)} | {clock(times[-1] - start)} | {med:.1f}s | "
                   f"{', '.join(gaps[:6]) + (' ...' if len(gaps) > 6 else '') or '-'} |")
    out += ["", "## Trouble"]
    for name, pattern in TROUBLE.items():
        hits = [ts for ts, logger, msg in events if re.search(pattern, f"{logger} {msg}")]
        if hits:
            out.append(f"- {name}: {len(hits)} (first {clock(hits[0] - start)})")
    runtime = [msg for _, logger, msg in events if logger == "timeline" and msg.startswith("TIMELINE")]
    progress = [f"{clock(ts - start)} {msg}" for ts, logger, msg in events if logger == "swarm" and msg.startswith("progress")]
    if progress:
        out += ["", "## Progress (runtime, per minute)"] + [f"- {p[:260]}" for p in progress]
    if runtime:
        out += ["", "## Runtime timeline report"] + [f"- {r[:600]}" for r in runtime]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("--gap", type=float, default=30.0)
    args = ap.parse_args(argv)
    print(analyse(parse(args.log), args.gap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
