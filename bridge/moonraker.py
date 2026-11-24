"""Moonraker/Klipper: aktive Spoolman-Spule setzen."""

from __future__ import annotations

import aiohttp


class Moonraker:
    def __init__(self, name: str, url: str, session: aiohttp.ClientSession):
        self.name = name
        self.url = url.rstrip("/")
        self.http = session

    async def set_active_spool(self, spool_id: int) -> None:
        async with self.http.post(f"{self.url}/server/spoolman/spool_id",
                                  json={"spool_id": spool_id}) as r:
            if r.status >= 400:
                raise RuntimeError(f"Moonraker {self.name} -> {r.status}: {(await r.text())[:200]}")
