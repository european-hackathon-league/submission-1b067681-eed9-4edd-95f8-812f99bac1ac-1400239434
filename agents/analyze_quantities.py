# OWNER: (name)   ROLE: analyzer   NEEDS: tender.position, tender.clarification   WRITES: results["quantities"]
from swarm import Agent, Phase, register


@register
class QuantityAnalyzer(Agent):
    """Final quantity per position = tender qty overridden by the latest non-withdrawn clarification."""
    phase = Phase.ANALYZE

    async def run(self) -> None:
        # TODO: read ctx.board.query("tender.position"/"tender.clarification"), let the model resolve which clarification
        # applies to which position, apply deterministic ordering by date in code, then:
        # await self.ctx.board.set("quantities", {...})
        return None
