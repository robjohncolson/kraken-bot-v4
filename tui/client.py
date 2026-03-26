"""HTTP client for fetching dashboard snapshots."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:58392"
DEFAULT_TIMEOUT = 5


class DashboardClient:
    """Async-friendly wrapper around the bot's JSON API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # -- low level -----------------------------------------------------------

    def _get_sync(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    async def _get(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, path)

    # -- individual endpoints ------------------------------------------------

    async def fetch_health(self) -> dict[str, Any]:
        return await self._get("/api/health")

    async def fetch_portfolio(self) -> dict[str, Any]:
        return await self._get("/api/portfolio")

    async def fetch_positions(self) -> dict[str, Any]:
        return await self._get("/api/positions")

    async def fetch_beliefs(self) -> dict[str, Any]:
        return await self._get("/api/beliefs")

    async def fetch_stats(self) -> dict[str, Any]:
        return await self._get("/api/stats")

    async def fetch_reconciliation(self) -> dict[str, Any]:
        return await self._get("/api/reconciliation")

    # -- composite -----------------------------------------------------------

    async def fetch_snapshot(self) -> dict[str, dict[str, Any]]:
        """Fetch all endpoints in parallel.  Failed endpoints → ``{}``."""
        results = await asyncio.gather(
            self.fetch_health(),
            self.fetch_portfolio(),
            self.fetch_positions(),
            self.fetch_beliefs(),
            self.fetch_stats(),
            self.fetch_reconciliation(),
            return_exceptions=True,
        )
        keys = ("health", "portfolio", "positions", "beliefs", "stats", "reconciliation")
        snapshot: dict[str, dict[str, Any]] = {}
        for key, result in zip(keys, results):
            snapshot[key] = {} if isinstance(result, BaseException) else result
        return snapshot

    # -- helpers -------------------------------------------------------------

    @property
    def sse_url(self) -> str:
        return f"{self._base_url}/sse/updates"
