"""Prüft auf GitHub, ob eine neuere Version der Bridge verfügbar ist."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import aiohttp

from . import __version__

log = logging.getLogger(__name__)

VERSION_URL = "https://raw.githubusercontent.com/derBonsaii/spool-bridge/main/bridge/__init__.py"
CHECK_EVERY = 6 * 3600  # alle 6 Stunden


def parse_version(text: str) -> Optional[str]:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text or "")
    return m.group(1) if m else None


def is_newer(candidate: str, current: str) -> bool:
    def key(v: str):
        return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))
    try:
        return key(candidate) > key(current)
    except Exception:
        return False


class UpdateChecker:
    def __init__(self, session: aiohttp.ClientSession, url: str = VERSION_URL):
        self.http = session
        self.url = url
        self.latest: Optional[str] = None

    @property
    def update_available(self) -> Optional[str]:
        return self.latest if self.latest and is_newer(self.latest, __version__) else None

    async def check_once(self) -> None:
        try:
            async with self.http.get(self.url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    self.latest = parse_version(await r.text()) or self.latest
        except Exception as e:  # kein Internet o.ä. – einfach später nochmals
            log.debug("Update-Prüfung fehlgeschlagen: %s", e)
        if self.update_available:
            log.info("Update verfügbar: %s (installiert: %s)", self.latest, __version__)

    async def run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(CHECK_EVERY)
