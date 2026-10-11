# OWNER: (name)   ROLE: submitter   NEEDS: results["bid"], results["brief"]   WRITES: enters the bid on the tender portal, signs
"""Enters the computed bid in the tender portal's submission wizard and signs it with the TAN from the webmail.

The model drives the wizard (browser 0) from the brief's submission instructions and a table of the values to enter;
`fill_form` sets many fields per step. When the wizard asks for the signing TAN the model calls `get_signing_tan`,
which runs a short browse on the mail browser (3) to read the newest TAN mail. Runs even if earlier phases failed:
then every position without data is omitted ("partial submission"), the bid is still signed.

Time savers (the submission reserve shrinks with them, see swarm.context.submit_reserve):
- WizardPrep: near the end of collection the tender portal's main tab logs in and opens the bid wizard (entering
  nothing), so the submitter starts at the wizard's first step.
- At submit start the mail browser's main tab logs in and opens the inbox, so the TAN lookup only opens the newest mail.
- check_entries compares the summary page with the values to enter in code (one step instead of re-reading pages).
"""
import os
import time
from decimal import Decimal
from swarm.normalize import de_number, norm_ref
import asyncio

from pydantic import BaseModel, Field

from swarm import Agent, Phase, register, timeline
from swarm.blackboard import Evidence
from swarm.browser_agent import ActionResult, browse, read_current_page, BrowserSession
from swarm.context import FINAL_MARGIN_S
from swarm.extract import brief_section
from swarm.sites import site_for

PREP_LEAD_S = float(os.environ.get("SWARM_PREP_LEAD_S", 150))   # warm the wizard up this long before collection ends


class CheckParams(BaseModel):
    note: str = Field(default="", description="optional: which page/step this is (e.g. summary)")


def expected_values(bid: dict) -> dict[str, list[str]]:
    """What the portal's summary should show, per position (material, quantity, unit price) and for the bid."""
    answer = bid.get("answer") or {}
    out: dict[str, list[str]] = {}
    lines = {l["position"]: l for l in bid.get("lines") or []}
    for e in answer.get("positions") or []:
        disp = (lines.get(e["position"]) or {}).get("display") or {}
        out[e["position"]] = [str(e["material_number"]), str(e["quantity"]),
                              disp.get("unit_price") or de_number(Decimal(str(e["unit_price"])))]
    if answer.get("positions"):
        out["total"] = [bid.get("total_display") or de_number(Decimal(str(answer.get("total_net") or 0)))]
    return out


def check_page(text: str, expected: dict[str, list[str]]) -> list[str]:
    """Expected values not found on the page (numbers compared without spaces; case-insensitive)."""
    flat = norm_ref(text)
    return [f"{where}: {value}" for where, values in expected.items() for value in values
            if value and norm_ref(value) not in flat]


class TanParams(BaseModel):
    new_request: bool = Field(default=False, description="True only after explicitly requesting a replacement TAN in the portal")
    requested_at: str = Field(default="", description="when you requested the TAN (as shown), if known")


class ReceiptParams(BaseModel):
    quote: str = Field(description="Exact text on the current portal page confirming the bid was submitted, not saved as draft")
    reference: str = Field(description="Tender reference for this receipt")


def bid_table(bid: dict, tender: dict) -> str:
    lines = []
    answer = bid.get("answer") or {}
    by_pos = {e["position"]: e for e in answer.get("positions") or []}
    for line in bid.get("lines") or []:
        pos = line["position"]
        if pos not in by_pos:
            lines.append(f"- Position {pos}: OMIT this position (use the omit/skip option for it).")
            continue
        e, disp = by_pos[pos], line.get("display") or {}
        req = {k: v for k, v in (line.get("configuration") or {}).items() if v} or line.get("required") or {}
        extra = ", ".join(f"{label} {disp[k]}" for k, label in (("base_unit_price", "base price per piece before discount"),
                                                                  ("surcharge_per_unit", "surcharges per piece"),
                                                                  ("packaging_per_unit", "packaging per piece"),
                                                                  ("rebate_total", "project bonus for the whole position"))
                          if k in disp)
        lines.append(f"- Position {pos}: configuration bore {req.get('bore')}, stroke {req.get('stroke')}, mounting "
                     f"{req.get('mounting')}, certification {req.get('certification')} -> material {e['material_number']}; "
                     f"quantity {e['quantity']} pieces; net unit price {disp.get('unit_price')} EUR; lead time "
                     f"{e['lead_time_weeks']} weeks; breakdown (optional fields): {extra or 'leave empty'}")
    known = {line["position"] for line in bid.get("lines") or []}
    for p in (tender or {}).get("positions") or []:
        if str(p.get("position")) not in known:
            lines.append(f"- Position {p.get('position')}: OMIT this position.")
    return "\n".join(lines) or "- (no position data: omit every position the form shows)"


@register
class BidSubmitter(Agent):
    phase = Phase.SUBMIT
    browser = 0

    async def run(self) -> None:
        ctx = self.ctx
        if ctx.task.get("submission", {}).get("kind") == "api":
            return
        bid = ctx.board.get("bid") or ctx.board.get("bid_draft") or {"answer": {"decision": "bid", "positions": []},
                                                                      "lines": []}
        facts = ctx.board.get("brief") or {}
        if bid.get("validation_errors"):
            self.log.warning("Bid validation issues require repair in the wizard: %s", bid["validation_errors"])
        answer = bid["answer"]
        tender_site = await site_for(ctx, "tender")
        mail_site = (ctx.board.get("sites") or {}).get("mail")
        site = ctx.site(tender_site)
        creds = site.get("credentials") or {}
        contact = ctx.board.get("mailbox") or ""
        started = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        decision = answer.get("decision", "bid")
        goal = (
            f"Site (tender portal): {site['url']}\nLogin: username {creds.get('username')!r}, password {creds.get('password')!r}\n\n"
            f"SUBMISSION INSTRUCTIONS FROM BRIEF:\n{brief_section(ctx.brief, 'Submission', 'Abgabe') or ctx.brief[-2500:]}\n\n"
            f"YOUR JOB: submit exactly ONE signed bid for tender {answer.get('tender_reference') or facts.get('tender_reference')}.\n"
            f"Bidder company: {facts.get('bidder_company')}; VAT id (USt-IdNr.): {facts.get('bidder_vat_id')}"
            f"{'; contact e-mail: ' + contact if contact else ''}.\n"
            f"Decision: {'main offer (bid)' if decision == 'bid' else 'no bid (declaration of non-participation)'}.\n"
            f"Discount percent: {de_number(Decimal(str(answer.get('discount_percent') or 0)))}. Net total: {bid.get('total_display', '0,00')} EUR "
            f"(enter numbers in German format exactly as given).\n"
            f"POSITIONS (form may ask in different order: always read which position it shows):\n"
            f"{bid_table(bid, ctx.board.get('tender') or {})}\n\n"
            "How to work: log in, accept cookies, open tender, register interest if needed, open bid submission. Close "
            "survey popups with their x button. Each step saves only with its continue/next button; going back clears "
            "later steps -> get each step right before continuing. fill_form sets many fields of a step in one action: "
            "per position ONE fill_form with all configuration fields in order and apply_button = the button applying "
            "the selection (it is pressed between dependent selects for you; do not set them one per step), then ONE "
            "fill_form for material, quantity, prices, lead time with then_click = the continue button. Never use "
            "buttons that add extra positions: every tender position has its own wizard step. Unsure -> check step result with "
            "read_page (selects may default to wrong option). Fill actual editable controls only. Read-only outputs and "
            "group labels are not checkboxes. If a form reports invalid format, inspect the entered number format first. "
            "Do not repeatedly search for a nonexistent control. Acknowledge tender documents, tick nothing the brief does "
            "not ask for (e.g. own terms). Form wants signing TAN -> request it in portal, call get_signing_tan, enter "
            "returned TAN right away (TAN valid few minutes; new request invalidates old one). Review summary, confirm "
            "binding submission (accept browser dialog). done once portal confirms submission; report "
            "confirmation/receipt text. Call confirm_submission with an exact quote from the portal receipt before done. "
            "If get_signing_tan reports pending, keep this wizard open and poll that tool; do not request another TAN. "
            "A failed attempt may already have submitted: inspect current portal status before any new binding action. "
            "On the summary/review page call check_entries once (it compares the page with the values above); fix "
            "what it reports missing before confirming; no need to read_page the summary yourself.")
        prepared = ctx.shared.get("wizard_ready")
        if prepared:
            goal += (f"\n\nPREPARED: this tab is already logged in and in the bid wizard for this tender (nothing entered "
                     f"yet). What the preparation saw: {str(prepared)[:400]}")

        tan_task = None
        warm_mail = asyncio.create_task(self.warm_mailbox(mail_site))
        expected = expected_values(bid)

        def tan_tool(tools):
            @tools.action("Check the current page (bid summary) against the values to enter; lists what is missing.",
                          param_model=CheckParams)
            async def check_entries(params: CheckParams, browser_session: BrowserSession):
                text = await read_current_page(browser_session)
                missing = check_page(text, expected)
                await ctx.board.set("submission_check", {"missing": missing, "checked": sum(map(len, expected.values())),
                                                         "note": params.note})
                timeline.mark("entries checked", missing=len(missing))
                if missing:
                    return ActionResult(extracted_content="NOT on this page (fix before confirming): " + "; ".join(missing),
                                        long_term_memory="check_entries: missing " + "; ".join(missing)[:300])
                return ActionResult(extracted_content=f"All {sum(map(len, expected.values()))} expected values are on "
                                                      "this page.", long_term_memory="check_entries: all values present")

            @tools.action("Confirm acceptance using an exact receipt quote on the current portal page.", param_model=ReceiptParams)
            async def confirm_submission(params: ReceiptParams, browser_session: BrowserSession):
                from swarm.normalize import norm_ref
                from urllib.parse import urlsplit
                current_url = await browser_session.get_current_page_url()
                if urlsplit(current_url).netloc != urlsplit(site["url"]).netloc:
                    return ActionResult(error="Acceptance must be confirmed on the tender portal")
                observed = await read_current_page(browser_session)
                quote = " ".join(params.quote.split())
                expected = answer.get("tender_reference") or facts.get("tender_reference")
                if not expected or norm_ref(params.reference) != norm_ref(expected):
                    return ActionResult(error="Receipt reference does not match this tender")
                if len(quote) < 12 or quote not in " ".join(observed.split()):
                    return ActionResult(error="Receipt quote is not on the current page; read the portal confirmation")
                receipt = {"accepted": True, "reference": expected, "quote": quote,
                           "url": await browser_session.get_current_page_url()}
                await ctx.board.set("submission", receipt)
                await ctx.board.post(Evidence("submission.receipt", tender_site, receipt, source=receipt["url"], author=self.name))
                timeline.mark("accepted submission", left=round(ctx.seconds_left()))
                timeline.current().log_report()        # the run (and this process) ends within seconds
                return ActionResult(extracted_content="Receipt recorded; finish now", is_done=True, success=True)

            @tools.action("Read newest signing TAN from our webmail (call right after requesting TAN).",
                          param_model=TanParams)
            async def get_signing_tan(params: TanParams):
                nonlocal tan_task, mail_site
                if not mail_site:
                    try:
                        mail_site = await site_for(ctx, "mail")
                    except Exception as error:
                        return ActionResult(error=f"mail website discovery failed ({type(error).__name__}); retry after inspecting site capabilities")
                async def retrieve():
                    mail = ctx.site(mail_site)
                    mc = mail.get("credentials") or {}
                    found: list[dict] = []

                    async def keep(rec):
                        found.append(rec)
                        await ctx.board.post(Evidence(kind="mail.tan" if rec["kind"] == "mail.tan" else "mail.page",
                                                      site=mail_site, data=rec["data"], source=rec["source"],
                                                      author=self.name, note=rec["note"]))
                    job = (f"Site (webmail): {mail['url']}\nLogin: username {mc.get('username')!r}, password "
                           f"{mc.get('password')!r}\n\nYOUR JOB: signing TAN for tender "
                           f"{answer.get('tender_reference')} just requested (at {params.requested_at or 'just now'}; "
                           f"session started {started}). Open inbox without any search filter (clear the search box if "
                           "the list looks empty; new TAN mails appear without searching), open NEWEST "
                           "signing-TAN message for this tender, read_page it, record kind 'mail.tan' with data "
                           "{\"tan\": \"...\", \"received\": \"...\", \"request\": \"...\"} copied exactly, then done "
                           "stating the TAN. Recording mail.tan completes this lookup immediately; record it once.")
                    async with ctx.lock("main_tab.3"):      # after the inbox warm-up, in the same (logged-in) tab
                        out = await browse(ctx, job, browser=3, site_id=mail_site, on_record=keep, max_steps=12,
                                           author=f"{self.name}.tan", stop_reserve=FINAL_MARGIN_S,
                                           dedupe_reads=False, stop_on_record="mail.tan")
                    tans = [r["data"].get("tan") for r in found if r["kind"] != "page" and r["data"].get("tan")]
                    text = f"TAN: {tans[-1]}" if tans else f"No TAN recorded. Mail agent said: {out['summary'][:300]}"
                    if tans:
                        timeline.mark("TAN received")
                    return text

                if timeline.mark("TAN requested", left=round(ctx.seconds_left())):
                    timeline.current().log_report()   # the run ends soon after the TAN is entered: report it now

                if params.new_request and tan_task is not None:
                    tan_task.cancel()
                    await asyncio.gather(tan_task, return_exceptions=True)
                    tan_task = None
                if tan_task is None:
                    tan_task = asyncio.create_task(retrieve())
                try:
                    text = await asyncio.wait_for(asyncio.shield(tan_task), timeout=3)
                except TimeoutError:
                    return ActionResult(extracted_content="TAN lookup pending. Keep the portal open. Poll get_signing_tan; do not request a new TAN.")
                except Exception as error:
                    tan_task = None
                    return ActionResult(error=f"TAN lookup failed ({type(error).__name__}); retry the lookup")
                if text.startswith("No TAN"):
                    tan_task = None
                return ActionResult(extracted_content=text, long_term_memory=text)

        try:
            note = ""
            for attempt in range(5):           # an agent that stopped early (errors) gets another go while time allows
                try:
                    out = await browse(ctx, goal + note, browser=self.browser, site_id=tender_site, max_steps=60,
                                       author=self.name, stop_reserve=FINAL_MARGIN_S, extra_tools=tan_tool,
                                       max_actions_per_step=5, flash=False,   # binding, signed: keep full reasoning
                                       careful=True)          # the popup guard never touches the wizard's own forms
                except Exception as error:
                    ctx.board.error(self.name, f"submission attempt {attempt + 1}: {type(error).__name__}")
                    out = {"summary": f"browser interrupted: {type(error).__name__}", "done": False, "steps": 0}
                await ctx.board.set("submission_attempt", {"summary": out["summary"], "done": out["done"], "steps": out["steps"],
                                                   "attempt": attempt + 1})
                self.log.info("submission attempt %d finished (done=%s): %s", attempt + 1, out["done"], out["summary"][:500])
                closed = ctx.shared.get("run_closed")
                if closed and not (ctx.board.get("submission") or {}).get("accepted"):
                    # the platform ended the run: the binding submission went through if the wizard reached signing
                    signed = "TAN requested" in timeline.current().milestones
                    receipt = {"accepted": signed, "reference": answer.get("tender_reference") or facts.get("tender_reference"),
                               "evidence": f"platform closed the run with {closed['left']}s left"
                                           + (" after the TAN was requested" if signed else " before any TAN request")}
                    await ctx.board.set("submission", receipt)
                    if signed:
                        timeline.mark("accepted submission", left=closed["left"], via="platform closed the run")
                    self.log.warning("run closed by the platform: %s", receipt["evidence"])
                    break
                if (ctx.board.get("submission") or {}).get("accepted") or ctx.seconds_left() < FINAL_MARGIN_S + 45:
                    break
                note = (f"\n\nNOTE: previous attempt stopped early ({out['summary'][:300]!r}). Browser may be mid-wizard: "
                        "check current page first, continue from there.")
        finally:
            for task in (tan_task, warm_mail):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def warm_mailbox(self, mail_site: str | None) -> None:
        """Log the mail browser's main tab in and open the inbox while the wizard is filled, so the TAN lookup later
        only opens the newest message (the submitter waits for the TAN otherwise)."""
        ctx = self.ctx
        try:
            mail_site = mail_site or await site_for(ctx, "mail")
            mail = ctx.site(mail_site)
            mc = mail.get("credentials") or {}
            async with ctx.lock("main_tab.3"):
                with timeline.span("submit.prep", "mailbox"):
                    out = await browse(ctx, f"Site (webmail): {mail['url']}\nLogin: username {mc.get('username')!r}, "
                                            f"password {mc.get('password')!r}\n\nYOUR JOB: log in if needed, accept the "
                                            "cookie wall, close popups, pass any access gate, open the inbox with NO "
                                            "search filter (clear a search left in the search box, so new mails "
                                            "show). Open no message. Then done.",
                                       browser=3, site_id=mail_site, max_steps=6, author=f"{self.name}.mailbox",
                                       stop_reserve=FINAL_MARGIN_S, dedupe_reads=False)
            if out.get("done"):
                timeline.mark("mailbox ready")
        except Exception as error:  # noqa: BLE001 - only a time saver: the TAN lookup logs in itself if needed
            self.log.info("mailbox warm-up failed: %s", error)


@register
class WizardPrep(Agent):
    """Near the end of collection (PREP_LEAD_S before cutoff, or once every site's first pass is done), the tender
    portal's main tab logs in, opens the tender and its bid wizard and stops at the first step without entering
    anything. The submitter then starts in the wizard (ctx.shared["wizard_ready"] shortens the submission reserve)."""
    phase = Phase.COLLECT
    background = True
    browser = 0

    async def run(self) -> None:
        ctx = self.ctx
        # the tender scraper works in the same main tab: never before its pass is over
        while ctx.board.get("done.tender") is None or (
                ctx.time_left() > PREP_LEAD_S and not all(ctx.board.get(f"done.{r}") is not None
                                                          for r in ("spec", "erp", "mail"))):
            await asyncio.sleep(2)
        if ctx.task.get("submission", {}).get("kind") == "api" or ctx.time_left() < 45:
            return
        site_id = await site_for(ctx, "tender")
        site = ctx.site(site_id)
        creds = site.get("credentials") or {}
        reference = (ctx.board.get("brief") or {}).get("tender_reference")
        async with ctx.lock("main_tab.0"):
            with timeline.span("submit.prep", "wizard"):
                out = await browse(
                    ctx, f"Site (tender portal): {site['url']}\nLogin: username {creds.get('username')!r}, password "
                         f"{creds.get('password')!r}\n\nYOUR JOB: prepare the bid submission for tender {reference}: "
                         "log in if needed, accept necessary cookies, open the tender, register interest if the portal "
                         "requires it, open its bid submission (Angebotsabgabe) wizard and stop at its FIRST step. Do "
                         "NOT fill in, select, tick or submit anything in the wizard. done describing the wizard's "
                         "steps/sections as shown.", browser=0, site_id=site_id, max_steps=8, author=self.name,
                    stop_extra=0, careful=True)
        if out.get("done"):
            ctx.shared["wizard_ready"] = out.get("summary") or "wizard open"
            timeline.mark("wizard ready", left=round(ctx.seconds_left()))
        self.log.info("wizard prep done=%s: %s", out.get("done"), str(out.get("summary"))[:200])
