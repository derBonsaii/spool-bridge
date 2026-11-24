"""Schlanker Spoolman-API-Client (REST v1)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)

NFC_FIELD = "nfc_id"
BAMBU_IDX_FIELD = "bambu_idx"


def normalize_uid(uid: str) -> str:
    """'04-a1:B2 c3' -> '04A1B2C3'"""
    return re.sub(r"[^0-9A-Fa-f]", "", uid or "").upper()


def _decode_extra(value: Any) -> Any:
    """Spoolman speichert Extra-Felder als JSON-kodierte Strings."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class Spoolman:
    def __init__(self, base_url: str, session: aiohttp.ClientSession):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.http = session

    async def _req(self, method: str, path: str, **kw) -> Any:
        async with self.http.request(method, self.base + path, **kw) as r:
            if r.status >= 400:
                text = await r.text()
                raise RuntimeError(f"Spoolman {method} {path} -> {r.status}: {text[:300]}")
            if r.content_type == "application/json":
                return await r.json()
            return None

    # ------------------------------------------------------------ Extra-Felder
    async def ensure_fields(self) -> None:
        """Legt die benötigten Extra-Felder an, falls sie fehlen."""
        wanted = {
            "spool": {NFC_FIELD: "NFC Tag"},
            "filament": {BAMBU_IDX_FIELD: "Bambu Filament-ID"},
        }
        for entity, fields in wanted.items():
            existing = {f["key"] for f in await self._req("GET", f"/field/{entity}")}
            for key, name in fields.items():
                if key not in existing:
                    await self._req(
                        "POST",
                        f"/field/{entity}/{key}",
                        json={"name": name, "field_type": "text"},
                    )
                    log.info("Spoolman: Extra-Feld %s.%s angelegt", entity, key)

    # ------------------------------------------------------------ Spulen
    async def spools(self) -> list[dict]:
        return await self._req("GET", "/spool", params={"allow_archived": "false"})

    async def spool(self, spool_id: int) -> dict:
        return await self._req("GET", f"/spool/{spool_id}")

    async def find_by_uid(self, uid: str) -> Optional[dict]:
        uid = normalize_uid(uid)
        for s in await self.spools():
            tag = _decode_extra((s.get("extra") or {}).get(NFC_FIELD))
            if tag and normalize_uid(str(tag)) == uid:
                return s
        return None

    async def link_uid(self, spool_id: int, uid: str) -> None:
        uid = normalize_uid(uid)
        # Tag darf nur an einer Spule hängen.
        for s in await self.spools():
            tag = _decode_extra((s.get("extra") or {}).get(NFC_FIELD))
            if tag and normalize_uid(str(tag)) == uid and s["id"] != spool_id:
                await self._set_extra(s, NFC_FIELD, "")
        await self._set_extra(await self.spool(spool_id), NFC_FIELD, uid)

    async def _set_extra(self, spool: dict, key: str, value: str) -> None:
        extra = dict(spool.get("extra") or {})
        extra[key] = json.dumps(value)
        await self._req("PATCH", f"/spool/{spool['id']}", json={"extra": extra})

    async def set_location(self, spool_id: int, location: str) -> None:
        await self._req("PATCH", f"/spool/{spool_id}", json={"location": location})

    async def move_all(self, from_location: str, to_location: str, except_id: int | None = None) -> None:
        for s in await self.spools():
            if s.get("location") == from_location and s["id"] != except_id:
                await self.set_location(s["id"], to_location)


def spool_label(spool: dict) -> str:
    f = spool.get("filament") or {}
    vendor = (f.get("vendor") or {}).get("name", "")
    parts = [p for p in (vendor, f.get("material"), f.get("name")) if p]
    return f"#{spool['id']} " + " ".join(parts)
