# OWNER: (name)   ROLE: analyzer   NEEDS: erp.*, results["requirements"]   WRITES: results["materials"]
from swarm import Agent, Phase, register


@register
class MaterialMatcher(Agent):
    """Pick the ACTIVE ERP material matching each position EXACTLY (bore, stroke, pressure, mounting, certification);
    none -> 'no bid' for that position. Needs requirements: use board.wait_for(key='requirements') or run after."""
    phase = Phase.ANALYZE

    async def run(self) -> None:
        return None  # TODO
