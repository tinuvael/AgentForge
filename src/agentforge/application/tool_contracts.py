"""Typed public tool catalog shared by MCP and bounded Director clients."""

from dataclasses import dataclass

from pydantic import BaseModel

from agentforge.application.contracts import (
    AgentsPage,
    CandidateSuggestions,
    Capabilities,
    CleanupCodingArguments,
    CouncilArguments,
    DelegateArguments,
    DelegateCouncilArguments,
    NoArguments,
    PageArguments,
    ProjectsPage,
    Status,
    SuggestArguments,
    TaskArguments,
    TaskProgress,
    TaskSnapshot,
    WorkersPage,
)
from agentforge.coding.models import CodingDiff, CodingResult
from agentforge.councils.models import CouncilSnapshot


@dataclass(frozen=True)
class ToolContract:
    name: str
    operation: str
    description: str
    arguments: type[BaseModel]
    response: type[BaseModel]
    read_only: bool = True


TOOL_CONTRACTS = (
    ToolContract(
        "suggest_workers",
        "suggest_workers",
        (
            "Advisory eligible Workers and scoped capability/policy evidence for one "
            "Project and Agent; no inference, ranking, reservation or selection."
        ),
        SuggestArguments,
        CandidateSuggestions,
    ),
    ToolContract(
        "get_coding_workspace",
        "get_coding_workspace",
        (
            "Inspect one Task's persisted coding identity and bounded factual "
            "observations; private workspace paths omitted. Validation captures "
            "remain untrusted text."
        ),
        TaskArguments,
        CodingResult,
    ),
    ToolContract(
        "get_coding_diff",
        "get_coding_diff",
        (
            "Retrieve the current central bounded coding diff, including new "
            "files and explicit truncation."
        ),
        TaskArguments,
        CodingDiff,
    ),
    ToolContract(
        "cleanup_coding_workspace",
        "cleanup_coding_workspace",
        (
            "Explicitly remove a terminal Task's managed worktree, retaining "
            "its task branch. Destructive; supply both matching IDs."
        ),
        CleanupCodingArguments,
        CodingResult,
        False,
    ),
    ToolContract(
        "delegate_council",
        "delegate_council",
        "Durably submit the same request independently on 2 to 16 explicitly selected "
        "Workers; returns promptly, without judging or sharing answers.",
        DelegateCouncilArguments,
        CouncilSnapshot,
        False,
    ),
    ToolContract(
        "get_council",
        "get_council",
        "Ordered participant states and completed answers; terminal means all Tasks "
        "are terminal, including mixed outcomes. No request text, trace or reasoning.",
        CouncilArguments,
        CouncilSnapshot,
    ),
    ToolContract(
        "cancel_council",
        "cancel_council",
        "Cancel remaining participants via queued/cooperative Task cancellation; "
        "terminal participants remain unchanged. Safe to repeat.",
        CouncilArguments,
        CouncilSnapshot,
        False,
    ),
    ToolContract(
        "agentforge_status",
        "status",
        "Compact control-plane status; no Worker probes.",
        NoArguments,
        Status,
    ),
    ToolContract(
        "describe_capabilities",
        "capabilities",
        "Factual capabilities and limitations; no routing advice.",
        NoArguments,
        Capabilities,
    ),
    ToolContract(
        "list_projects",
        "list_projects",
        "Page of registered Projects; roots for the trusted local director; "
        "Git not probed.",
        PageArguments,
        ProjectsPage,
    ),
    ToolContract(
        "list_workers",
        "list_workers",
        "Configured Worker capabilities in ID order; no endpoints, health "
        "probes or ranking.",
        PageArguments,
        WorkersPage,
    ),
    ToolContract(
        "list_agents",
        "list_agents",
        "Actual Agent definitions, allowed tools and runtime limits.",
        PageArguments,
        AgentsPage,
    ),
    ToolContract(
        "delegate_task",
        "delegate_task",
        "Submit an explicit Project/Agent/Worker request; return queued "
        "durable identity promptly.",
        DelegateArguments,
        TaskSnapshot,
        False,
    ),
    ToolContract(
        "get_task",
        "get_task",
        "Durable snapshot and completed answer; no trace or reasoning.",
        TaskArguments,
        TaskSnapshot,
    ),
    ToolContract(
        "watch_task",
        "watch_task",
        "Observe one Task using request-scoped MCP progress when a progressToken "
        "is supplied; otherwise return latest safe metadata promptly. No replay "
        "or answer content. Cancelling this watch does not cancel the Task.",
        TaskArguments,
        TaskProgress,
    ),
    ToolContract(
        "cancel_task",
        "cancel_task",
        "Cancel queued work or request cooperative running cancellation; "
        "never force-kill remote inference.",
        TaskArguments,
        TaskSnapshot,
        False,
    ),
)
