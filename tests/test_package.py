"""Verify that the installed bootstrap package and its areas are importable."""

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "agentforge",
        "agentforge.api",
        "agentforge.mcp",
        "agentforge.core",
        "agentforge.agents",
        "agentforge.providers",
        "agentforge.workers",
        "agentforge.projects",
        "agentforge.tools",
        "agentforge.telemetry",
        "agentforge.db",
        "agentforge.web",
    ],
)
def test_package_import(module_name: str) -> None:
    assert import_module(module_name).__name__ == module_name
