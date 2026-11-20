"""Shipped Agent composition shared by services and operator discovery."""

from agentforge.agents import GENERAL_AGENT, REPO_EXPLORER
from agentforge.agents.models import Agent
from agentforge.coding.tools import CODER
from agentforge.execution.ssh_codex_runner import AGENT_ID, PROMPT

SSH_CODEX_SYNTHETIC = Agent(
    id=AGENT_ID,
    name="SSH Codex synthetic check",
    description=f"Only the fixed read-only request is accepted: {PROMPT}",
    system_prompt=PROMPT,
)


def shipped_agents(*, coding_enabled: bool = False, ssh_codex_enabled: bool = False):
    readonly = (GENERAL_AGENT, REPO_EXPLORER)
    if ssh_codex_enabled:
        readonly += (SSH_CODEX_SYNTHETIC,)
    return (*readonly, CODER) if coding_enabled else readonly
