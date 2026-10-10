# OWNER: (name)   ROLE: submitter   NEEDS: results["bid"], results["brief"]   WRITES: enters the bid on the tender portal, signs
"""Enters the computed bid in the tender portal's submission wizard and signs it with the TAN from the webmail.

The model drives the wizard (browser 0) from the brief's submission instructions and a table of the values to enter;
`fill_form` sets many fields per step. When the wizard asks for the signing TAN the model calls `get_signing_tan`,
which runs a short browse on the mail browser (3) to read the newest TAN mail. Runs even if earlier phases failed:
then every position without data is omitted ("partial submission"), the bid is still signed.
"""
import time

from pydantic import BaseModel, Field

from swarm import Agent, Phase, register
from swarm.blackboard import Evidence
from swarm.browser_agent import ActionResult, browse
from swarm.context import FINAL_MARGIN_S
from swarm.extract import brief_section
from swarm.sites import site_for


class TanParams(BaseModel):
    requested_at: str = Field(default="", description="when you requested the TAN (as shown), if known")


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
        bid = ctx.board.get("bid") or {"answer": {"decision": "bid", "positions": []}, "lines": []}
        facts = ctx.board.get("brief") or {}
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
            f"Discount percent: {answer.get('discount_percent')}. Net total: {bid.get('total_display', '0,00')} EUR "
            f"(enter numbers in German format exactly as given).\n"
            f"POSITIONS (form may ask in different order: always read which position it shows):\n"
            f"{bid_table(bid, ctx.board.get('tender') or {})}\n\n"
            "How to work: log in, accept cookies, open tender, register interest if needed, open bid submission. Close "
            "survey popups with their x button. Each step saves only with its continue/next button; going back clears "
            "later steps -> get each step right before continuing. fill_form sets many fields of a step in one action: "
            "cascading selects -> pass apply_button (button applying the selection); set configuration first, then "
            "material, quantity, prices, lead time; then_click continue button. Unsure -> check step result with "
            "read_page (selects may default to wrong option). Acknowledge tender documents, tick nothing the brief does "
            "not ask for (e.g. own terms). Form wants signing TAN -> request it in portal, call get_signing_tan, enter "
            "returned TAN right away (TAN valid few minutes; new request invalidates old one). Review summary, confirm "
            "binding submission (accept browser dialog). done once portal confirms submission; report "
            "confirmation/receipt text.")

        def tan_tool(tools):
            @tools.action("Read newest signing TAN from our webmail (call right after requesting TAN).",
                          param_model=TanParams)
            async def get_signing_tan(params: TanParams):
                if not mail_site:
                    return ActionResult(error="mail website unknown")
                mail = ctx.site(mail_site)
                mc = mail.get("credentials") or {}
                found: list[dict] = []

                async def keep(rec):
                    found.append(rec)
                    await ctx.board.post(Evidence(kind="mail.tan" if rec["kind"] != "page" else "mail.page",
                                                  site=mail_site, data=rec["data"], source=rec["source"],
                                                  author=self.name, note=rec["note"]))
                job = (f"Site (webmail): {mail['url']}\nLogin: username {mc.get('username')!r}, password "
                       f"{mc.get('password')!r}\n\nYOUR JOB: signing TAN for tender "
                       f"{answer.get('tender_reference')} just requested (at {params.requested_at or 'just now'}; "
                       f"session started {started}). Open inbox (new TAN mails appear without searching), open NEWEST "
                       "signing-TAN message for this tender, read_page it, record kind 'mail.tan' with data "
                       "{\"tan\": \"...\", \"received\": \"...\", \"request\": \"...\"} copied exactly, then done "
                       "stating the TAN.")
                out = await browse(ctx, job, browser=3, site_id=mail_site, on_record=keep, max_steps=10,
                                   author=f"{self.name}.tan", stop_reserve=FINAL_MARGIN_S,
                                   dedupe_reads=False)       # a new TAN request may update the same message page
                tans = [r["data"].get("tan") for r in found if r["kind"] != "page" and r["data"].get("tan")]
                text = f"TAN: {tans[-1]}" if tans else f"No TAN recorded. Mail agent said: {out['summary'][:300]}"
                return ActionResult(extracted_content=text, long_term_memory=text)

        note = ""
        for attempt in range(3):           # an agent that stopped early (errors) gets another go while time allows
            out = await browse(ctx, goal + note, browser=self.browser, site_id=tender_site, max_steps=60,
                               author=self.name, stop_reserve=FINAL_MARGIN_S, extra_tools=tan_tool,
                               max_actions_per_step=5, flash=False,   # binding, signed: keep full reasoning
                               careful=True)          # the popup guard never touches the wizard's own forms
            await ctx.board.set("submission", {"summary": out["summary"], "done": out["done"], "steps": out["steps"],
                                               "attempt": attempt + 1})
            self.log.info("submission attempt %d finished (done=%s): %s", attempt + 1, out["done"], out["summary"][:500])
            if out["done"] or ctx.seconds_left() < FINAL_MARGIN_S + 90:
                break
            note = (f"\n\nNOTE: previous attempt stopped early ({out['summary'][:300]!r}). Browser may be mid-wizard: "
                    "check current page first, continue from there.")
