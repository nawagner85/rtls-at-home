"""HTTP client for an RTLS@Home engine (ingest protocol v1, see docs/protocol.md)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class CannotConnect(Exception):
    """The engine could not be reached or answered with an error."""


class InvalidAuth(Exception):
    """The engine rejected the token."""


class EngineClient:
    """Talks to one engine."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def hello(self) -> dict[str, Any]:
        """Protocol version and token check."""
        return await self._request("GET", "/api/ingest/hello")

    async def ingest(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Send one batch; the reply carries `wanted` keys and `tracked` results."""
        return await self._request("POST", "/api/ingest", batch)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(5):
                async with self._session.request(method, self._url + path, json=body, headers=self._headers) as resp:
                    if resp.status == 401:
                        raise InvalidAuth
                    if resp.status != 200:
                        raise CannotConnect(f"HTTP {resp.status}")
                    return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
