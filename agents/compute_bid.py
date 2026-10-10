# OWNER: (name)   ROLE: calculator (plain code, no model)   NEEDS: quantities, materials, discount, erp.*   WRITES: results["bid"]
from decimal import ROUND_HALF_UP, Decimal  # noqa: F401  use Decimal; round half-up once at the end

from swarm import Agent, Phase, register


@register
class BidCalculator(Agent):
    """Deterministic pricing/lead time/allocation per the brief's rules. Keep rounding rules in tested pure functions."""
    phase = Phase.COMPUTE

    async def run(self) -> None:
        return None  # TODO: await self.ctx.board.set("bid", {...})
