"""Fail immediately if any test attempts live networking, including DNS."""

import os
import socket
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.projects.service import ProjectRegistry


def pytest_collection_modifyitems(items):
    for item in items:
        if item.get_closest_marker("windows") and os.name != "nt":
            item.add_marker(
                pytest.mark.skip(reason="Requires native Windows; not emulated")
            )
        if item.get_closest_marker("posix") and os.name == "nt":
            item.add_marker(
                pytest.mark.skip(reason="POSIX-specific security regression")
            )


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    original_connect = socket.socket.connect

    def connect(sock, *args, **kwargs):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("Tests must not connect to live network services")
        return original_connect(sock, *args, **kwargs)

    def resolve(*args, **kwargs):
        raise AssertionError("Tests must not perform live DNS lookups")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def database(tmp_path):
    url = URL.create("sqlite", database=str(tmp_path / "registry.sqlite"))
    engine = create_database_engine(url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield engine, url
    engine.dispose()


@pytest.fixture
def registry(database, tmp_path):
    engine, _ = database
    return ProjectRegistry(
        ProjectRepository(create_session_factory(engine)), base_directory=tmp_path
    )
