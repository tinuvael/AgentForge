"""Shipped Agent composition shared by services and operator discovery."""

from agentforge.agents import GENERAL_AGENT, REPO_EXPLORER
from agentforge.coding.tools import CODER


def shipped_agents(*, coding_enabled: bool = False):
    readonly = (GENERAL_AGENT, REPO_EXPLORER)
    return (*readonly, CODER) if coding_enabled else readonly
