import asyncio
import socket
from unittest.mock import AsyncMock

import httpcore
import pytest

from pricehunter.core.network import PublicNetworkBackend, public_addresses


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "2002:0a00:0001::",
    ],
)
async def test_dns_private_and_transition_addresses_rejected(monkeypatch, address):
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(httpcore.ConnectError):
        await public_addresses("api.ebay.com", 443)


async def test_network_pins_checked_ip_without_second_dns_lookup(monkeypatch):
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    stream = object()
    connect = AsyncMock(return_value=stream)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    assert await PublicNetworkBackend().connect_tcp("api.ebay.com", 443, timeout=1) is stream
    assert connect.call_args.args[:2] == ("8.8.8.8", 443)
    assert lookup.await_count == 1
