# OWNER: (name)   ROLE: analyzer   NEEDS: spec.*   WRITES: results["requirements"]
from swarm import Agent, Phase, register


@register
class RequirementAnalyzer(Agent):
    """Per position requirements from the CURRENT spec revision with scoped amendments applied."""
    phase = Phase.ANALYZE

    async def run(self) -> None:
        return None  # TODO
