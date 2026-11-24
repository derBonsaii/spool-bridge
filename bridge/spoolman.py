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
BAMBU_UUID_FIELD = "bambu_uuid"
BAMBU_VENDOR = "Bambu Lab"


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
            "spool": {NFC_FIELD: "NFC Tag", BAMBU_UUID_FIELD: "Bambu Spulen-UUID"},
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

    async def set_remaining(self, spool_id: int, grams: float) -> None:
        await self._req("PATCH", f"/spool/{spool_id}", json={"remaining_weight": round(grams, 1)})

    # ------------------------------------------------------------ Bambu-RFID-Spulen
    async def find_by_bambu_uuid(self, uuid: str) -> Optional[dict]:
        uuid = uuid.upper()
        for s in await self._req("GET", "/spool", params={"allow_archived": "true"}):
            val = _decode_extra((s.get("extra") or {}).get(BAMBU_UUID_FIELD))
            if val and str(val).upper() == uuid:
                return s
        return None

    async def _bambu_vendor_id(self) -> int:
        for v in await self._req("GET", "/vendor"):
            if (v.get("name") or "").lower() == BAMBU_VENDOR.lower():
                return v["id"]
        v = await self._req("POST", "/vendor", json={"name": BAMBU_VENDOR})
        log.info("Spoolman: Hersteller '%s' angelegt", BAMBU_VENDOR)
        return v["id"]

    async def find_or_create_bambu_filament(self, info: dict) -> dict:
        """info: name, material, color_hex, idx, weight, diameter, density, extruder_temp, bed_temp"""
        vendor_id = await self._bambu_vendor_id()
        for f in await self._req("GET", "/filament"):
            if (f.get("vendor") or {}).get("id") != vendor_id:
                continue
            idx = _decode_extra((f.get("extra") or {}).get(BAMBU_IDX_FIELD))
            if idx == info["idx"] and (f.get("color_hex") or "").upper() == info["color_hex"]:
                return f
        body = {
            "name": info["name"],
            "vendor_id": vendor_id,
            "material": info["material"],
            "color_hex": info["color_hex"],
            "density": info["density"],
            "diameter": info["diameter"],
            "weight": info["weight"],
            "extra": {BAMBU_IDX_FIELD: json.dumps(info["idx"])},
        }
        if info.get("extruder_temp"):
            body["settings_extruder_temp"] = info["extruder_temp"]
        if info.get("bed_temp"):
            body["settings_bed_temp"] = info["bed_temp"]
        f = await self._req("POST", "/filament", json=body)
        log.info("Spoolman: Filament '%s' #%s angelegt", info["name"], info["color_hex"])
        return f

    async def create_bambu_spool(self, filament_id: int, uuid: str, location: str,
                                 remaining: Optional[float]) -> dict:
        body = {
            "filament_id": filament_id,
            "location": location,
            "extra": {BAMBU_UUID_FIELD: json.dumps(uuid.upper())},
        }
        if remaining is not None:
            body["remaining_weight"] = round(remaining, 1)
        return await self._req("POST", "/spool", json=body)

    async def move_all(self, from_location: str, to_location: str, except_id: int | None = None) -> None:
        for s in await self.spools():
            if s.get("location") == from_location and s["id"] != except_id:
                await self.set_location(s["id"], to_location)


def spool_label(spool: dict) -> str:
    f = spool.get("filament") or {}
    vendor = (f.get("vendor") or {}).get("name", "")
    parts = [p for p in (vendor, f.get("material"), f.get("name")) if p]
    return f"#{spool['id']} " + " ".join(parts)
