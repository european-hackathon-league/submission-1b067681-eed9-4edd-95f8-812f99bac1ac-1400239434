"""Shared swarm infrastructure. Agents live in `agents/`; see docs/SWARM.md."""
from swarm.agent import Agent, Phase, register
from swarm.blackboard import Blackboard, Evidence
from swarm.context import Context

__all__ = ["Agent", "Phase", "register", "Blackboard", "Evidence", "Context"]
