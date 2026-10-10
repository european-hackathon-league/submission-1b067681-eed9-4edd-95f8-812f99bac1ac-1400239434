# OWNER: (shared)   ROLE: coordinator (model with reasoning; no browser of its own)
# NEEDS: every analyst's results   WRITES: results["gaps"], follow-up evidence through the scrapers' sites
"""Decides what the scrapers collect next, so the run ends COLLECT with the data the bid needs instead of with what
the first pass happened to save.

While the first pass runs, the coordinator watches the board. As soon as a site's first pass and its analysis are done,
code lists the GAPS on that site: concrete facts the bid needs that no saved page holds (a position without a
matching material, a material without status / price scales / stock, a commercial key from the Q&A without its mail,
a usable-travel requirement without its technical release, no discount approval, ...). The model (with reasoning) turns
the gaps into a few focused jobs, one browser agent each, grouping what one agent can do in one go and skipping gaps the
saved pages show do not exist. Jobs run at once, in the site's browser (main tab when free, new tabs otherwise). After
each job the affected analyses run again on the new pages, which may reveal the next gaps.

No gap is worked on twice, every job states exactly what to read (so no two agents collect the same thing), and a page
another agent already saved is not opened again (read_page returns the saved text). Follow-ups stop when nothing is
missing, nothing new can be tried, or the collect time is used up.
"""
import asyncio
import json

from agents import analyze_discount, analyze_materials, analyze_quantities, analyze_requirements
from agents.compute_bid import position_keys, valid_condition
from swarm import Agent, Phase, register
from swarm.context import SUBMIT_RESERVE_S
from swarm.extract import pages
from swarm.normalize import norm_ref, to_datetime
from swarm.scraper import run_job

ROUND_MIN_S = 100        # a follow-up job needs at least this much collect time
JOB_STEPS = 16
MAX_JOBS = 6             # per planning call
MAX_JOBS_PER_ROLE = 3
ANALYSIS_S = 60          # jobs stop this long before collect ends, so the analyses can still run on their pages
SETTLE_S = 45           # a site whose analysis has not finished this long after its first pass is treated as done

READY = {"tender": ("done.tender", "quantities"), "spec": ("done.spec", "requirements_final"),
         "erp": ("done.erp", "materials"), "mail": ("done.mail", "mail_final")}

KEY_SECTIONS = {"konditionsgruppe": ("condition_rules", "condition rules (Konditionsregeln)"),
                "packaging_tariff": ("packaging_tariffs", "packaging tariff"),
                "delivery_key": ("delivery_commitments", "delivery commitment"),
                "setup_pool": ("setup_pools", "setup pool fee"),
                "bonus_plan": ("project_bonus", "project bonus bands")}

SYSTEM = """You coordinate browser agents that collect data for a priced bid. Each site has a scraper agent that already
did a first pass. You get the GAPS (facts the bid still needs), what each site holds, and the topics already saved.
Write follow-up jobs: one job = one browser agent on one site, a few sentences: what to open/search and what to
read_page with which topic label (e.g. 'erp prices <material>'). Put gaps one agent can do in one go into one job (at
most 3 gaps per job); separate jobs run in parallel. Name exact identifiers (material numbers, keys, references).
Skip a gap when the saved topics/summaries show the data does not exist on the site (e.g. no Q&A at all). Never ask for
a page already saved unless it lacked the needed part (then say which part: e.g. open the quantity-scale register).
Reply JSON: {"jobs": [{"role": "tender|spec|erp|mail", "gaps": ["gap id", ...], "job": "..."}], "skipped": ["gap id"]}"""

SITES = {"tender": "tender portal: tender overview, documents (bill of quantities), bidder Q&A",
         "spec": "buyer's portal: technical specification revisions and requirement tables",
         "erp": "our ERP: material search help, material records (technical data, status, lead times), price "
                "conditions with quantity scales, stock per storage location",
         "mail": "our webmail: approvals and release mails (discount, condition rules, technical release, packaging, "
                 "delivery, setup pools, project bonus, FX, price indices)"}


def _attrs(req: dict) -> str:
    return ", ".join(f"{k} {req.get(k)}" for k in ("bore", "stroke", "stroke_kind", "pressure", "mounting",
                                                     "certification") if req.get(k))


def find_gaps(board, ready: set[str], saved_topics: dict[str, list[str]]) -> list[dict]:
    """The facts the bid still needs, per site, from the results of the analyses that are ready. Plain code."""
    gaps: dict[str, dict] = {}

    def gap(role: str, gid: str, need: str) -> None:
        if role in ready:
            gaps.setdefault(gid, {"id": gid, "role": role, "need": need})

    brief = board.get("brief") or {}
    tender = board.get("tender") or {}
    positions = tender.get("positions") or []
    requirements = board.get("requirements") or {}
    materials = board.get("materials") or {}
    erp = board.get("erp") or {}
    mail = board.get("mail") or {}
    erp_materials = {norm_ref(k): v for k, v in (erp.get("materials") or {}).items()}
    scenario = to_datetime(brief.get("scenario_date"))
    if "tender" in ready and not positions:
        gap("tender", "tender positions", "bill of quantities / position overview: positions, quantities, units, "
                                          "requirement references")
    if positions and not any(w in t for t in saved_topics.get("tender", []) for w in ("q&a", "frage", "question", "clarif", "bieter")):
        gap("tender", "tender q&a", "bidder Q&A of the tender, all pages (if the tender has any)")
    if (board.get("discount") or {}).get("source") in ("no valid approval found", "latest approval invalid"):
        gap("mail", "mail discount", f"sales director's discount approval for {brief.get('tender_reference')} "
                                     "(inbox and archive)")
    clarifications = tender.get("clarifications") or []
    for p in positions:
        pos = str(p.get("position")).strip()
        ref = p.get("requirement_ref")
        req = next((v for k, v in requirements.items() if norm_ref(k) == norm_ref(ref)), None)
        if req is None:
            gap("spec", f"spec requirement {ref}", f"requirement {ref} of the CURRENT specification revision (bore, "
                                                   "stroke, pressure, mounting, certification, catalogue group)")
            continue
        group = req.get("kataloggruppe") or ""
        if req.get("stroke_kind") == "usable":
            releases = mail.get("technical_release") or {}
            if not releases:
                gap("mail", "mail technical release",
                    f"technical release mail with end reserves per stroke end (revision {req.get('revision')}"
                    f"{', key ' + str(req.get('reserve_key')) if req.get('reserve_key') else ''})")
        for key, value in position_keys(clarifications, pos)["keys"].items():
            section, label = KEY_SECTIONS.get(key, (None, None))
            if section and norm_ref(value) not in {norm_ref(k) for k in (mail.get(section) or {})}:
                gap("mail", f"mail {section} {norm_ref(value)}", f"{label} mail for key {value}")
        m = materials.get(pos) or {}
        if not m:
            gap("erp", f"erp search {group or ref}", f"Find materials for requirement {ref}: {_attrs(req)}")
            continue
        mat = m.get("material")
        if not mat:
            searched = any(norm_ref(group) and norm_ref(group) in norm_ref(t) for t in saved_topics.get("erp", []))
            in_group = [v["material"] for v in erp_materials.values()
                        if norm_ref(v.get("kataloggruppe")) == norm_ref(group)] if group else []
            if not searched and not in_group:
                gap("erp", f"erp search {group or ref}", f"search result list of catalogue group {group} "
                                                         f"(requirement {ref}: {_attrs(req)})")
            elif m.get("reason") == "only blocked matches":
                gap("erp", f"erp status {group or ref}", f"status (active/blocked, from record and status legend) of "
                                                         f"{[c['material'] for c in m.get('candidates') or []]}: all "
                                                         f"read as blocked for requirement {ref}")
            else:
                gap("erp", f"erp records {group or ref}", f"record pages (technical data incl. stroke and "
                                                          f"certification, status) of materials {in_group or 'of group ' + group}: "
                                                          f"none matched requirement {ref} ({_attrs(req)}) on the pages "
                                                          "read")
            continue
        info = erp_materials.get(norm_ref(mat)) or {}
        missing = [a for a in m.get("unverified") or []]
        if info.get("lead_time_production_weeks") in (None, ""):
            missing.append("lead times")
        if position_keys(clarifications, pos)["keys"].get("packaging_tariff") and not info.get("pieces_per_carton"):
            missing.append("pieces per carton")
        if missing:
            gap("erp", f"erp record {mat}", f"record of material {mat} (all tabs, status legend): "
                                            f"{', '.join(dict.fromkeys(missing))} not read")
        conds = [c for c in erp.get("conditions") or [] if norm_ref(c.get("material")) == norm_ref(mat)]
        cond = valid_condition(conds, mat, scenario)
        if not conds:
            gap("erp", f"erp prices {mat}", f"price condition records of material {mat} with quantity scales")
        elif cond is None:
            gap("erp", f"erp prices {mat}", f"price condition of material {mat} valid on {brief.get('scenario_date')} "
                                            f"(read: {[(c.get('valid_from'), c.get('valid_to')) for c in conds]}; "
                                            "other condition records/periods)")
        elif not cond.get("scales"):
            gap("erp", f"erp prices {mat}", f"quantity scales of the valid price condition of {mat} (open the "
                                            "quantity-scale register)")
        elif norm_ref(cond.get("currency") or "EUR") != "EUR" and \
                norm_ref(cond.get("currency")) not in {norm_ref(k) for k in (mail.get("fx_fixing") or {})}:
            gap("mail", f"mail fx {norm_ref(cond.get('currency'))}", f"FX fixing mail for {cond.get('currency')} "
                                                                     f"(key {cond.get('fx_key')})")
        if norm_ref(mat) not in {norm_ref(k) for k in (erp.get("stock") or {})}:
            gap("erp", f"erp stock {mat}", f"stock of material {mat} at storage-location level")
        index = info.get("metal_index")
        if index and norm_ref(index) not in {norm_ref(k) for k in (mail.get("price_indices") or {})}:
            gap("mail", f"mail price index {norm_ref(index)}", f"metal price index mail for {index} valid on "
                                                               f"{brief.get('scenario_date')}")
    return list(gaps.values())


async def refresh(ctx, roles: set[str]) -> None:
    """Run the analyses that depend on new pages of `roles` again (each failure is only logged)."""
    steps = []
    if "tender" in roles:
        steps += [analyze_quantities.refresh]
    if roles & {"tender", "spec"} and pages(ctx, "spec"):
        steps += [analyze_requirements.refresh]
    if "mail" in roles:
        steps += [analyze_discount.refresh]
    if pages(ctx, "erp"):
        steps += [analyze_materials.refresh]
    for step in steps:
        try:
            await step(ctx)
        except Exception as error:
            ctx.board.error("CollectionCoordinator", f"refresh {step.__module__}: {error}")


@register
class CollectionCoordinator(Agent):
    phase = Phase.COLLECT

    def __init__(self, ctx):
        super().__init__(ctx)
        self.tried: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.running: dict[asyncio.Task, str] = {}      # follow-up job -> its site role
        self.history: list[dict] = []
        self.first_pass_end: dict[str, float] = {}

    def ready(self) -> set[str]:
        """Sites whose first pass and analysis are done (gaps can be judged)."""
        return {role for role, keys in READY.items() if all(self.ctx.board.get(k) is not None for k in keys)}

    def settled(self) -> set[str]:
        """Ready sites, plus sites whose analysis has not finished SETTLE_S after the first pass (it failed)."""
        now = self.ctx.elapsed()
        for role, (done_key, _) in READY.items():
            if self.ctx.board.get(done_key) is not None:
                self.first_pass_end.setdefault(role, now)
        return self.ready() | {r for r, t in self.first_pass_end.items() if now - t > SETTLE_S}

    def saved_topics(self) -> dict[str, list[str]]:
        return {role: sorted({str(e.data.get("topic", "")).lower() for e in pages(self.ctx, role)} - {""})
                for role in READY}

    async def plan(self, gaps: list[dict]) -> list[dict]:
        board = self.ctx.board
        summaries = {k: str(v)[:600] for k, v in board.results.items() if k.endswith(".summary")}
        prompt = {"tender_reference": (board.get("brief") or {}).get("tender_reference"),
                  "scenario_date": (board.get("brief") or {}).get("scenario_date"),
                  "gaps": gaps, "sites": {r: SITES[r] for r in {g["role"] for g in gaps}},
                  "saved_topics": self.saved_topics(), "first_pass_summaries": summaries,
                  "earlier_followups": self.history[-8:], "site_tips": board.get("tips") or {}}
        reply = await self.ctx.llm.ask_json(SYSTEM, json.dumps(prompt, ensure_ascii=False, default=str), think=True)
        jobs, per_role = [], {}
        known = {g["id"]: g for g in gaps}
        for j in (reply or {}).get("jobs") or []:
            role = j.get("role")
            ids = [i for i in j.get("gaps") or [] if i in known and known[i]["role"] == role]
            if role not in READY or not ids or not str(j.get("job") or "").strip():
                continue
            if per_role.get(role, 0) >= MAX_JOBS_PER_ROLE or len(jobs) >= MAX_JOBS:
                continue
            per_role[role] = per_role.get(role, 0) + 1
            jobs.append({"role": role, "gaps": ids, "job": str(j["job"]).strip()})
        skipped = [i for i in (reply or {}).get("skipped") or [] if i in known]
        if skipped:
            self.log.info("gaps skipped by the model: %s", skipped)
        self.tried |= set(skipped)
        return jobs

    async def work(self, job: dict) -> None:
        reserve = SUBMIT_RESERVE_S + ANALYSIS_S
        try:
            out = await run_job(self.ctx, job["role"], job["job"], max_steps=JOB_STEPS, stop_reserve=reserve)
            self.history.append({"role": job["role"], "gaps": job["gaps"], "done": out.get("done"),
                                 "summary": str(out.get("summary"))[:300]})
            self.log.info("follow-up %s %s done=%s: %s", job["role"], job["gaps"], out.get("done"),
                          str(out.get("summary"))[:200])
        except Exception as error:
            self.ctx.board.error(self.name, f"follow-up {job['gaps']}: {error}")
            self.tried.difference_update(job["gaps"])
        if not locals().get("out", {}).get("done"):
            self.tried.difference_update(job["gaps"])
        await refresh(self.ctx, {job["role"]})

    async def run(self) -> None:
        try:
            while True:
                ready = self.settled()
                if self.ctx.time_left() > ROUND_MIN_S:
                    gaps = [g for g in find_gaps(self.ctx.board, ready, self.saved_topics()) if g["id"] not in self.tried and self.attempts.get(g["id"], 0) < 2]
                    busy = set(self.running.values())
                    gaps = [g for g in gaps if g["role"] not in busy]     # a site with follow-ups running: wait for them
                    if gaps:
                        self.log.info("gaps: %s", [g["id"] for g in gaps])
                        try:
                            jobs = await self.plan(gaps)
                        except Exception as error:
                            self.log.warning("planning failed: %s", error)
                            jobs = []
                        for job in jobs:
                            for gid in job["gaps"]:
                                self.attempts[gid] = self.attempts.get(gid, 0) + 1
                            self.tried.update(job["gaps"])
                            task = asyncio.create_task(self.work(job))
                            self.running[task] = job["role"]
                            task.add_done_callback(lambda t: self.running.pop(t, None))
                all_done = self.settled() == set(READY)
                if self.running:
                    await asyncio.wait(list(self.running), timeout=5)
                    continue
                pending = [g for g in find_gaps(self.ctx.board, ready, self.saved_topics())
                           if g["id"] not in self.tried and self.attempts.get(g["id"], 0) < 2]
                if (all_done and not pending) or self.ctx.time_left() <= ROUND_MIN_S:
                    gaps = find_gaps(self.ctx.board, set(READY), self.saved_topics())
                    await self.ctx.board.set("gaps", gaps)
                    self.log.info("collect complete; open gaps: %s", [g["id"] for g in gaps])
                    return
                await asyncio.sleep(5)
        finally:
            tasks = list(self.running)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
