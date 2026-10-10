# OWNER: (name)   ROLE: analyzer   NEEDS: mail.*, tender.meta   WRITES: results["discount"]
from swarm import Agent, Phase, register


@register
class DiscountAnalyzer(Agent):
    """Latest valid sales-director approval for this exact tender reference (authorised sender only), else 0%."""
    phase = Phase.ANALYZE

    async def run(self) -> None:
        return None  # TODO
