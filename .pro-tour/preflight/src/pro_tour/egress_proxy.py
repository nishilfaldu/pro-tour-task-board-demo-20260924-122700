"""Secret-free TLS tunnels for the fixed pristine-example preflight.

There is no TCP listener. Browser and provider callers mount different Unix
sockets. Only CONNECT to literal, code-owned DNS names on port 443 is accepted;
TLS remains end-to-end with the caller. This is not a configurable PR proxy.
"""

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path

POLICIES = {
    "public": frozenset({"example.com", "iana.org", "www.iana.org"}),
    "provider": frozenset({"api.typesafe.ai"}),
}


def destination(header: bytes, allowed: frozenset[str]) -> str:
    if len(header) > 8192 or not header.endswith(b"\r\n\r\n"):
        raise ValueError("Invalid proxy header")
    first = header.split(b"\r\n", 1)[0]
    match = re.fullmatch(rb"CONNECT ([a-z0-9.-]+):443 HTTP/1\.[01]", first)
    if not match or match[1].decode("ascii") not in allowed:
        raise ValueError("Destination denied")
    return match[1].decode("ascii")


def public_addresses(answers: list[tuple]) -> list[tuple]:
    """Reject the whole DNS answer on any private/reserved/mapped address.

    Connect to the validated numeric sockaddr, never resolve the hostname again.
    """
    if not answers:
        raise ValueError("No DNS answers")
    for family, kind, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or kind != socket.SOCK_STREAM
            or address[1] != 443
            or not ip.is_global
            or ip.is_multicast
            or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None)
        ):
            raise ValueError("Address denied")
    return answers


async def connect(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    answers = public_addresses(
        await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    )
    for family, kind, protocol, _, address in answers:
        sock = socket.socket(family, kind, protocol)
        sock.setblocking(False)
        try:
            # address[0] is already numeric; sock_connect does not redo DNS.
            await asyncio.wait_for(loop.sock_connect(sock, address), 5)
            return await asyncio.open_connection(sock=sock)
        except OSError:
            sock.close()
        except BaseException:
            sock.close()
            raise
    raise OSError("Allowed destination unavailable")


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def tunnel(reader, writer, allowed, semaphore, *, lifetime: float = 90) -> None:
    peer = None
    try:
        async with asyncio.timeout(lifetime), semaphore:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            host = destination(header, allowed)
            upstream, peer = await connect(host)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            tasks = [
                asyncio.create_task(relay(reader, peer)),
                asyncio.create_task(relay(upstream, writer)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (
        ValueError,
        OSError,
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        # No host, request, response, TLS payload, or headers are logged.
        writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
    finally:
        if peer:
            peer.close()
        writer.close()


async def serve() -> None:
    servers = []
    for channel, allowed in POLICIES.items():
        path = Path(f"/run/pro-tour/{channel}/proxy.sock")
        semaphore = asyncio.Semaphore(16)

        async def handler(reader, writer, policy=allowed, limit=semaphore):
            await tunnel(reader, writer, policy, limit)

        servers.append(await asyncio.start_unix_server(handler, str(path), limit=8192))
        os.chmod(path, 0o600)
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(serve())
