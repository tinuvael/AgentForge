"""Shipped Agent composition shared by services and operator discovery."""

from agentforge.agents import REPO_EXPLORER
from agentforge.coding.tools import CODER


def shipped_agents(*, coding_enabled: bool = False):
    return (REPO_EXPLORER, CODER) if coding_enabled else (REPO_EXPLORER,)
