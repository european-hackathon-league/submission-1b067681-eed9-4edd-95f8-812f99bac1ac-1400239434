"""Which website plays which role. Site ids and names differ between datasets, so the model maps them from the brief
once per run; the result is cached on the board as results["sites"] = {role: siteId}."""
import logging

log = logging.getLogger("sites")

ROLES = {
    "tender": "public procurement / tender portal: the tender, its bill of quantities, bidder Q&A, bid submission",
    "spec": "the buyer's supplier/customer portal holding the technical specification (Lastenheft) and its revisions",
    "erp": "our own ERP: materials, stock, lead times, price conditions",
    "mail": "our webmail / sales inbox: approvals, internal release mails, signing TANs",
}


async def resolve_roles(ctx) -> dict:
    """{role: siteId} for the four roles; asks the model once, validates against the task's site ids."""
    async with ctx.lock("sites"):
        known = ctx.board.get("sites")
        if known:
            return known
        sites = [{"siteId": s.get("siteId"), "url": s.get("url")} for s in ctx.task.get("access", [])]
        mapping = {}
        if len(sites) > 1:
            try:
                out = await ctx.llm.ask_json(
                    "Map each role to exactly one siteId. Reply JSON {role: siteId}.",
                    f"Roles: {ROLES}\nSites: {sites}\nBrief (start):\n{ctx.brief[:2500]}")
                mapping = {r: v for r, v in (out or {}).items() if r in ROLES and v in ctx.site_ids}
            except Exception as error:   # fall through to the single-site / partial mapping below
                log.warning("role mapping failed: %s", error)
        for role in ROLES:                # any role the model left open: a site whose id or URL names it
            if role not in mapping:
                for s in sites:
                    if role in f"{s['siteId']} {s['url']}".lower() and s["siteId"] not in mapping.values():
                        mapping[role] = s["siteId"]
                        break
        await ctx.board.set("sites", mapping)
        log.info("site roles: %s", mapping)
        return mapping


async def site_for(ctx, role: str) -> str:
    mapping = await resolve_roles(ctx)
    if role not in mapping:
        raise KeyError(f"no website found for role {role!r}: {mapping}")
    return mapping[role]
