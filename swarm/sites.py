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
    """Map capabilities at runtime; incomplete mappings are retried, observed contradictions can be corrected."""
    async with ctx.lock("sites"):
        known = ctx.board.get("sites") or {}
        if all(role in known for role in ROLES):
            return known
        sites = [{"siteId": s.get("siteId"), "url": s.get("url")} for s in ctx.task.get("access", [])]
        mapping = dict(known)
        observations = ctx.shared.setdefault("site_observations", {})
        for attempt in range(3):
            try:
                out = await ctx.llm.ask_json(
                    "Map roles to siteId using the brief and observations. Reply {tender: siteId, spec: siteId, "
                    "erp: siteId, mail: siteId, inspect: [siteId]}. Omit uncertain mappings and select up to two "
                    "sites to inspect when names are opaque. Multiple capabilities may share a site.",
                    f"Roles: {ROLES}\nSites: {sites}\nCurrent mapping: {mapping}\nObservations: {observations}\nBrief:\n{ctx.brief}")
                mapping.update({r: v for r, v in (out or {}).items() if r in ROLES and v in ctx.site_ids})
                await ctx.board.set("sites", mapping)
                if all(role in mapping for role in ROLES):
                    break
                inspect = [v for v in (out or {}).get("inspect", []) if v in ctx.site_ids and v not in observations][:2]
                if inspect and getattr(ctx, "pool", None):
                    from swarm.browser_agent import Tab, browse
                    for site_id in inspect:
                        site = ctx.site(site_id)
                        tab = Tab(ctx.site_ids.index(site_id) % 4)
                        creds = site.get("credentials") or {}
                        try:
                            result = await browse(ctx, f"Inspect this workspace to identify its business capabilities: {site['url']}. "
                                                  f"Login if necessary using {creds}. Read its headings and menus. "
                                                  "Report what information it holds; do not submit anything.",
                                                  tab=tab, site_id=site_id, max_steps=5, author="SiteOrientation")
                            observations[site_id] = result["summary"][:2500]
                        finally:
                            await tab.close()
            except Exception as error:
                log.warning("role mapping attempt failed: %s", type(error).__name__)
        await ctx.board.set("sites", mapping)
        log.info("site roles: %s", mapping)
        return mapping


async def site_for(ctx, role: str) -> str:
    mapping = await resolve_roles(ctx)
    if role not in mapping:
        raise KeyError(f"no website found for role {role!r}: {mapping}")
    return mapping[role]
