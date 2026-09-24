"""Secret-free GitHub gateway with fixed API/upload destinations and no TCP listener."""

import asyncio
import json
import os
import re
from pathlib import Path

import egress_proxy

from pro_tour.github_media import MEDIA_HOSTS

ALLOWED = MEDIA_HOSTS


async def main():
    # Observe only parsed CONNECT hostnames; never headers, payloads, or keys.
    original = egress_proxy.destination

    def audited(header, allowed):
        try:
            host = original(header, allowed)
        except ValueError:
            match = re.fullmatch(
                rb"CONNECT ([a-z0-9.-]{1,253}):443 HTTP/1\.[01]", header.split(b"\r\n", 1)[0]
            )
            host = "denied:" + match[1].decode() if match else "denied-or-malformed"
            raise
        finally:
            with Path("/tmp/media-egress.jsonl").open("a") as stream:
                stream.write(json.dumps({"destination": host}) + "\n")
        return host

    egress_proxy.destination = audited
    limit = asyncio.Semaphore(16)

    async def handler(reader, writer):
        await egress_proxy.tunnel(reader, writer, ALLOWED, limit, lifetime=150)

    path = "/run/pro-tour/media/proxy.sock"
    server = await asyncio.start_unix_server(handler, path, limit=8192)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
