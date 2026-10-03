"""HTTP client for an RTLS@Home engine (ingest protocol v1, see docs/protocol.md)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class CannotConnect(Exception):
    """The engine could not be reached or answered with an error."""


class InvalidAuth(Exception):
    """The engine rejected the token."""


class NotReady(CannotConnect):
    """A picture the engine cannot draw yet (503: its base layers still being drawn after a start, or setup mode);
    ask again after `retry_after` seconds (its Retry-After; 2 s, the engine's, when absent or not a number)."""

    def __init__(self, retry_after: str | None) -> None:
        try:
            self.retry_after = max(0.5, float(retry_after))
        except (TypeError, ValueError):
            self.retry_after = 2.0
        super().__init__(f"HTTP 503, ready in {self.retry_after:g} s")


class EngineClient:
    """Talks to one engine."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    @property
    def url(self) -> str:
        """The engine's address (its page, for the devices' "Visit" link)."""
        return self._url

    async def hello(self) -> dict[str, Any]:
        """Protocol version and token check."""
        return await self._request("GET", "/api/ingest/hello")

    async def ingest(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Send one batch; the reply carries `wanted` keys and `tracked` results."""
        return await self._request("POST", "/api/ingest", batch)

    async def render(self, path: str, params: dict[str, str] | None = None) -> bytes:
        """A picture of the house (PNG; spec 2026-10-02 house renders), e.g. `/api/ingest/render/house.png` with
        `{"show": "<key>,<key>"}`. 15 s: the engine draws it on request, on a Raspberry Pi. NotReady (a
        CannotConnect) on 503."""
        return await self._request("GET", path, params=params, timeout=15, picture=True)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *,
                       params: dict[str, str] | None = None, timeout: float = 5, picture: bool = False) -> Any:
        try:
            async with asyncio.timeout(timeout):
                async with self._session.request(method, self._url + path, json=body, params=params,
                                                 headers=self._headers) as resp:
                    if resp.status == 401:
                        raise InvalidAuth
                    if resp.status == 503 and picture:
                        raise NotReady(resp.headers.get("Retry-After"))
                    if resp.status != 200:
                        raise CannotConnect(f"HTTP {resp.status}")
                    return await resp.read() if picture else await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
