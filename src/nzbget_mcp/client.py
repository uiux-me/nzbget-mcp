"""Minimal async JSON-RPC client for the NZBGet API.

NZBGet exposes JSON-RPC at ``<base-url>/jsonrpc`` behind HTTP basic auth.
Only positional parameters are supported and every parameter is mandatory,
so callers pass params in the documented order.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from .config import Settings


class NzbGetError(RuntimeError):
    """Raised when NZBGet cannot be reached or returns an RPC error."""


class NzbGetAuthError(NzbGetError):
    """Raised when NZBGet rejects the configured credentials."""


class NzbGetClient:
    """Thin wrapper around NZBGet's JSON-RPC endpoint."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._ids = itertools.count(1)
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(settings.username, settings.password),
            timeout=settings.timeout,
            verify=settings.verify_ssl,
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._major_version: int | None = None

    async def call(self, method: str, *params: Any) -> Any:
        """Invoke an NZBGet RPC method and return its result."""
        payload = {
            "version": "1.1",
            "id": next(self._ids),
            "method": method,
            "params": list(params),
        }
        try:
            response = await self._client.post(self._settings.rpc_url, json=payload)
        except httpx.HTTPError as exc:
            raise NzbGetError(
                f"Could not reach NZBGet at {self._settings.rpc_url}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise NzbGetAuthError(
                "NZBGet rejected the credentials "
                f"(HTTP {response.status_code}). Check NZBGET_USERNAME/NZBGET_PASSWORD."
            )
        if response.status_code >= 400:
            raise NzbGetError(
                f"NZBGet returned HTTP {response.status_code} for method {method!r}: "
                f"{response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise NzbGetError(
                f"NZBGet returned a non-JSON response for method {method!r}: "
                f"{response.text[:500]}"
            ) from exc

        error = body.get("error")
        if error:
            if isinstance(error, dict):
                message = error.get("message") or error
                code = error.get("code")
                raise NzbGetError(f"NZBGet error {code} on {method!r}: {message}")
            raise NzbGetError(f"NZBGet error on {method!r}: {error}")

        if "result" not in body:
            raise NzbGetError(f"NZBGet response for {method!r} contained no result")
        return body["result"]

    async def major_version(self) -> int:
        """Cached major version number, used to pick RPC call shapes."""
        if self._major_version is None:
            raw = str(await self.call("version"))
            digits = ""
            for char in raw.strip():
                if char.isdigit():
                    digits += char
                else:
                    break
            self._major_version = int(digits) if digits else 0
        return self._major_version

    async def edit_queue(self, command: str, param: str, ids: list[int]) -> bool:
        """Call editqueue, adapting to the pre-v18 four-argument signature."""
        if await self.major_version() >= 18:
            return bool(await self.call("editqueue", command, param, ids))

        offset = 0
        legacy_param = param
        if command.endswith("MoveOffset"):
            try:
                offset = int(param)
            except (TypeError, ValueError) as exc:
                raise NzbGetError(f"{command} needs an integer offset, got {param!r}") from exc
            legacy_param = ""
        return bool(await self.call("editqueue", command, offset, legacy_param, ids))

    async def aclose(self) -> None:
        await self._client.aclose()
