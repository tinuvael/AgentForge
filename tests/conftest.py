"""Fail immediately if any test attempts live networking, including DNS."""

import socket

import pytest


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
