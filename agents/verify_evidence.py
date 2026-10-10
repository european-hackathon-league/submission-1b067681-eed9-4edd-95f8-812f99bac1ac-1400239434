# OWNER: (name)   ROLE: verifier   NEEDS: all results   WRITES: results["verification"] (and may correct results)
from swarm import Agent, Phase, register


@register
class EvidenceVerifier(Agent):
    """Cross-check analyzers' results against the evidence: authority, tender reference, dates/timezones,
    superseded or withdrawn records. Flag or fix; never silently drop positions."""
    phase = Phase.VERIFY

    async def run(self) -> None:
        return None  # TODO
