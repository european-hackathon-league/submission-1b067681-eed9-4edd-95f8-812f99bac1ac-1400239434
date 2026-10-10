# OWNER: (name)   ROLE: submitter   NEEDS: results["bid"]   WRITES: enters the bid on tender-portal, signs with TAN
from swarm import Agent, Phase, register


@register
class BidSubmitter(Agent):
    """Fill the configurator (submission.kind == 'site') with results['bid'] and sign with the TAN from the mail
    evidence. Runs even when earlier phases failed: submit the best partial bid. Use swarm.browser_agent.browse
    for the model-driven form filling. Also keep a fallback so SOMETHING signed is submitted before time is out."""
    phase = Phase.SUBMIT
    browser = 0

    async def run(self) -> None:
        return None  # TODO
