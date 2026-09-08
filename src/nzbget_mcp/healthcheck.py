"""Container healthcheck: is the MCP port listening and NZBGet reachable?"""

from __future__ import annotations

import asyncio
import socket
import sys

from .client import NzbGetClient
from .config import load_settings


async def _check() -> None:
    settings = load_settings()

    if settings.transport != "stdio":
        host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
        with socket.create_connection((host, settings.port), timeout=5):
            pass

    client = NzbGetClient(settings)
    try:
        await client.call("version")
    finally:
        await client.aclose()


def main() -> int:
    try:
        asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
