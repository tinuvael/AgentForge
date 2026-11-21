"""Shipped Agent composition shared by services and operator discovery."""

from agentforge.agents import GENERAL_AGENT, REPO_EXPLORER
from agentforge.agents.models import Agent
from agentforge.coding.tools import CODER
from agentforge.execution.ssh_codex_repository import AGENT_ID as REPOSITORY_AGENT_ID
from agentforge.execution.ssh_codex_runner import AGENT_ID, PROMPT

SSH_CODEX_SYNTHETIC = Agent(
    id=AGENT_ID,
    name="SSH Codex synthetic check",
    description=f"Only the fixed read-only request is accepted: {PROMPT}",
    system_prompt=PROMPT,
)


SSH_CODEX_REPOSITORY = Agent(
    id=REPOSITORY_AGENT_ID,
    name="Read-only SSH Repository Pilot",
    description="Analyze the authorized committed sources of one Linux checkout.",
    system_prompt="Answer from the authorized committed sources and cite line ranges.",
    allowed_tools=(),
)


def shipped_agents(
    *,
    coding_enabled: bool = False,
    ssh_codex_enabled: bool = False,
    ssh_repository_enabled: bool = False,
):
    readonly = (GENERAL_AGENT, REPO_EXPLORER)
    if ssh_codex_enabled:
        readonly += (SSH_CODEX_SYNTHETIC,)
    if ssh_repository_enabled:
        readonly += (SSH_CODEX_REPOSITORY,)
    return (*readonly, CODER) if coding_enabled else readonly
