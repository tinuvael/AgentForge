"""Private event-loop socketpairs work; live services and DNS remain forbidden."""

import asyncio
import socket

import pytest


@pytest.mark.parametrize(
    "family,address",
    [
        (socket.AF_INET, ("127.0.0.1", 9)),
        (socket.AF_INET, ("192.0.2.1", 443)),
        (socket.AF_INET6, ("::1", 9)),
    ],
)
def test_live_connections_still_rejected(family, address):
    with socket.socket(family) as client:
        with pytest.raises(AssertionError, match="must not connect"):
            client.connect(address)


def test_dns_still_rejected():
    with pytest.raises(AssertionError, match="must not perform live DNS"):
        socket.getaddrinfo("example.invalid", 443)


def test_private_socketpair_and_asyncio_work_without_service_access():
    left, right = socket.socketpair()
    try:
        left.settimeout(1)
        right.settimeout(1)
        left.sendall(b"offline")
        assert right.recv(7) == b"offline"
    finally:
        left.close()
        right.close()
    asyncio.run(asyncio.sleep(0))
    with socket.socket() as client:
        with pytest.raises(AssertionError, match="must not connect"):
            client.connect(("127.0.0.1", 9))
