# OWNER: (name)   ROLE: verifier   NEEDS: all results   WRITES: results["verification"] (and may correct results)
"""Cross-checks before computing: the tender reference (mandatory for eligibility), positions vs quantities vs
materials, and which inputs are missing. Plain code; it never drops positions, it only reports (and fixes the
reference when the brief's and the portal's agree after normalisation).

results["verification"] = {"issues": [str], "reference": str}
"""
from swarm import Agent, Phase, register
from swarm.normalize import norm_ref, strip_invisible


@register
class EvidenceVerifier(Agent):
    phase = Phase.VERIFY

    async def run(self) -> None:
        board, issues = self.ctx.board, []
        brief = dict(board.get("brief") or {})
        tender = board.get("tender") or {}
        portal_ref = strip_invisible((tender.get("meta") or {}).get("reference"))
        ref = strip_invisible(brief.get("tender_reference"))
        if not ref and portal_ref and norm_ref(portal_ref) in norm_ref(self.ctx.brief):
            ref = portal_ref
            issues.append("reference taken from the tender portal (it also occurs in the brief)")
        elif ref and portal_ref and norm_ref(ref) != norm_ref(portal_ref):
            issues.append(f"reference mismatch: brief {ref!r} vs portal {portal_ref!r}; keeping the brief's")
        if not ref:
            issues.append("NO TENDER REFERENCE: the bid would be ineligible")
        brief["tender_reference"] = ref
        await board.set("brief", brief)
        positions = [str(p.get("position")) for p in tender.get("positions") or []]
        quantities, materials = board.get("quantities") or {}, board.get("materials") or {}
        for pos in positions:
            if (quantities.get(pos) or {}).get("quantity") in (None, 0):
                issues.append(f"{pos}: quantity missing")
            if not (materials.get(pos) or {}).get("material"):
                issues.append(f"{pos}: material missing ({(materials.get(pos) or {}).get('reason', 'not analysed')})")
        for key in ("discount", "mail", "erp", "requirements"):
            if board.get(key) is None:
                issues.append(f"{key}: not available")
        await board.set("verification", {"issues": issues, "reference": ref})
        self.log.info("verification: %s", issues)
