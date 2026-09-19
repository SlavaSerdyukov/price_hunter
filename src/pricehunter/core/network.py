import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Iterable

import certifi
import httpcore
import httpx

type SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


async def public_addresses(host: str, port: int) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
    if not addresses:
        raise httpcore.ConnectError("DNS returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise httpcore.ConnectError("Non-public destination rejected")
        if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
            raise httpcore.ConnectError("IPv6 transition destination rejected")
    return addresses


class PublicNetworkBackend(httpcore.AnyIOBackend):
    async def connect_tcp(  # noqa: ASYNC109 - implements the httpcore transport contract
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Connect to the checked numeric address: a second DNS lookup cannot rebind it.
        # httpcore still uses the original request hostname for TLS SNI/verification.
        async with asyncio.timeout(timeout):
            addresses = await public_addresses(host, port)
            for address in addresses:
                try:
                    return await super().connect_tcp(
                        address,
                        port,
                        timeout,
                        local_address,
                        socket_options,
                    )
                except httpcore.ConnectError:
                    continue
        raise httpcore.ConnectError("No public destination reachable")


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        # httpx does not expose a network_backend argument. Keep this one small
        # integration point pinned/tested with httpx 0.28 and httpcore 1.x.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            network_backend=PublicNetworkBackend(),
            max_connections=20,
            max_keepalive_connections=10,
            retries=0,
        )
