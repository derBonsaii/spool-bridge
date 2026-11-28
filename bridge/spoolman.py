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
HOME_FIELD = "home_location"
BAMBU_VENDOR = "Bambu Lab"


def normalize_uid(uid: str) -> str:
    """'04-a1:B2 c3' -> '04A1B2C3'"""
    return re.sub(r"[^0-9A-Fa-f]", "", uid or "").upper()


def spool_bambu_uuid(spool: dict) -> str:
    """Bambu-Spulen-UUID einer Spule (eigenes Feld oder Bambuddys 'tag'), sonst ''."""
    extra = spool.get("extra") or {}
    for key in (BAMBU_UUID_FIELD, "tag"):
        val = _decode_extra(extra.get(key))
        val = re.sub(r"[^0-9A-Fa-f]", "", str(val or "")).upper()
        if len(val) == 32 and val.strip("0"):
            return val
    return ""


def color_distance(a: str, b: str) -> float:
    try:
        ra, rb = bytes.fromhex((a or "")[:6]), bytes.fromhex((b or "")[:6])
    except ValueError:
        return 999.0
    if len(ra) != 3 or len(rb) != 3:
        return 999.0
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5


def match_bambu_filament(candidates: list[dict], info: dict) -> Optional[dict]:
    """Bestehendes Bambu-Filament ohne Bambu-ID zur RFID-Spule finden (z.B. aus SpoolmanDB)."""
    material = (info.get("material") or "").upper()
    line = (info.get("name") or "").lower()
    if line == material.lower():
        line = ""
    scored = []
    for f in candidates:
        idx = _decode_extra((f.get("extra") or {}).get(BAMBU_IDX_FIELD))
        if idx and idx != info.get("idx"):
            continue  # gehört zu einer anderen Bambu-Sorte
        if (f.get("material") or "").upper() != material:
            continue
        d = color_distance(f.get("color_hex"), info.get("color_hex"))
        line_ok = not line or line in (f.get("name") or "").lower()
        scored.append((d, line_ok, f))
    with_line = sorted((x for x in scored if x[1] and x[0] <= 60), key=lambda x: x[0])
    if with_line:
        return with_line[0][2]
    # Produktlinie fehlt im Namen: nur bei nahezu identischer, eindeutiger Farbe übernehmen
    close = [x for x in scored if x[0] <= 12]
    return close[0][2] if len(close) == 1 else None


def filament_label(f: dict) -> str:
    vendor = (f.get("vendor") or {}).get("name", "")
    material = (f.get("material") or "").strip()
    name = (f.get("name") or "").strip()
    if material and name.upper().startswith(material.upper()):
        material = ""
    text = " ".join(p for p in (material, name) if p)
    return f"{text} ({vendor})" if vendor else text


def uid_matches(a: str, b: str) -> bool:
    """Vergleicht zwei Tag-UIDs. Toleriert mit Nullen aufgefüllte UIDs (Bambu meldet
    z.B. '4D3E2A1B00000000', der PN532 liest '4D3E2A1B')."""
    a, b = normalize_uid(a), normalize_uid(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short) and set(long_[len(short):]) <= {"0"}


def spool_tags(spool: dict) -> list[str]:
    """Alle Tag-UIDs einer Spule (Extra-Feld 'NFC Tag', mehrere durch Komma getrennt)."""
    raw = _decode_extra((spool.get("extra") or {}).get(NFC_FIELD))
    return [normalize_uid(t) for t in str(raw or "").split(",") if normalize_uid(t)]


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
            "spool": {NFC_FIELD: "NFC Tag", BAMBU_UUID_FIELD: "Bambu Spulen-UUID",
                      HOME_FIELD: "Stammplatz"},
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
        for s in await self.spools():
            if any(uid_matches(t, uid) for t in spool_tags(s)):
                return s
        return None

    async def link_uid(self, spool_id: int, uid: str) -> None:
        """Tag an eine Spule hängen (zusätzlich zu vorhandenen, z.B. zweite Bambu-Seite).

        Ein Tag darf nur an einer Spule hängen: bei anderen Spulen wird er entfernt.
        """
        uid = normalize_uid(uid)
        for s in await self.spools():
            tags = spool_tags(s)
            if s["id"] != spool_id and any(uid_matches(t, uid) for t in tags):
                await self._set_extra(s, NFC_FIELD, ",".join(t for t in tags if not uid_matches(t, uid)))
        spool = await self.spool(spool_id)
        tags = spool_tags(spool)
        if not any(uid_matches(t, uid) for t in tags):
            await self._set_extra(spool, NFC_FIELD, ",".join(tags + [uid]))

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
        """Spule über die Bambu-Spulen-UUID finden – eigenes Feld oder Bambuddys 'tag'."""
        uuid = uuid.upper()
        for s in await self._req("GET", "/spool", params={"allow_archived": "true"}):
            if spool_bambu_uuid(s) == uuid:
                return s
        return None

    async def find_bambu_candidate(self, info: dict, slot_location: str):
        """Passende Spule für eine Bambu-RFID-Spule suchen, bevor eine neue angelegt wird.

        Reihenfolge: Bambu-UUID -> Tag-Kennung -> gerade per Scan diesem Slot zugewiesen ->
        eindeutig passende Bambu-Spule ohne UUID (z.B. über die Waage angelegt).
        Gibt (spool, grund) oder (None, grund) zurück.
        """
        spool = await self.find_by_bambu_uuid(info["uuid"])
        if spool is not None:
            return spool, "uuid"
        spools = await self.spools()
        free = [s for s in spools if not spool_bambu_uuid(s)]
        if info.get("tag_uid"):
            for s in free:
                if any(uid_matches(t, info["tag_uid"]) for t in spool_tags(s)):
                    return s, "tag"
        for s in free:
            if slot_location and s.get("location") == slot_location:
                return s, "slot"
        material = (info.get("material") or "").upper()
        cands = []
        for s in free:
            f = s.get("filament") or {}
            if ((f.get("vendor") or {}).get("name") or "").lower() != BAMBU_VENDOR.lower():
                continue
            if (f.get("material") or "").upper() != material:
                continue
            idx = _decode_extra((f.get("extra") or {}).get(BAMBU_IDX_FIELD))
            same_idx = bool(idx) and idx == info.get("idx")
            close = color_distance(f.get("color_hex"), info.get("color_hex")) <= 40
            if close and (same_idx or not idx):
                cands.append(s)
        if len(cands) == 1:
            return cands[0], "match"
        return None, ("ambiguous" if cands else "none")

    async def adopt_bambu_spool(self, spool: dict, uuid: str) -> None:
        """Bestehender Spule die Bambu-UUID nachtragen."""
        extra = dict(spool.get("extra") or {})
        extra[BAMBU_UUID_FIELD] = json.dumps(uuid.upper())
        await self._req("PATCH", f"/spool/{spool['id']}", json={"extra": extra})
        spool["extra"] = extra

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
        bambu = [f for f in await self.filaments()
                 if (f.get("vendor") or {}).get("id") == vendor_id]
        for f in bambu:
            idx = _decode_extra((f.get("extra") or {}).get(BAMBU_IDX_FIELD))
            if idx == info["idx"] and (f.get("color_hex") or "").upper() == info["color_hex"]:
                return f

        # Filament z.B. aus der SpoolmanDB übernommen: hat keine Bambu-ID, Farbcode kann leicht
        # abweichen. Passend = gleiches Material, Produktlinie im Namen, ähnlichste Farbe.
        match = match_bambu_filament(bambu, info)
        if match is not None:
            extra = dict(match.get("extra") or {})
            extra[BAMBU_IDX_FIELD] = json.dumps(info["idx"])
            await self._req("PATCH", f"/filament/{match['id']}", json={"extra": extra})
            match["extra"] = extra
            log.info("Spoolman: Bambu-Spule dem Filament '%s' zugeordnet", match.get("name"))
            return match

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

    async def filaments(self) -> list[dict]:
        return await self._req("GET", "/filament")

    async def create_spool(self, filament_id: int, location: str = "") -> dict:
        body: dict = {"filament_id": filament_id}
        if location:
            body["location"] = location
        return await self._req("POST", "/spool", json=body)

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

    async def place(self, spool: dict, location: str, is_printer_location) -> None:
        """Spule an einen Drucker-Ort setzen und den bisherigen Ort als Stammplatz merken."""
        current = (spool.get("location") or "").strip()
        if current == location:
            return
        body: dict = {"location": location}
        if current and not is_printer_location(current):
            extra = dict(spool.get("extra") or {})
            extra[HOME_FIELD] = json.dumps(current)
            body["extra"] = extra
        await self._req("PATCH", f"/spool/{spool['id']}", json=body)

    async def move_all(self, from_location: str, fallback: str, except_id: int | None = None,
                       skip_bambu: bool = False) -> None:
        """Alle Spulen an from_location zurück an ihren Stammplatz (sonst fallback).

        skip_bambu: Original-Bambu-Spulen nicht anfassen (z.B. wenn Bambuddy sie verwaltet).
        """
        for s in await self.spools():
            if skip_bambu and spool_bambu_uuid(s):
                continue
            if s.get("location") == from_location and s["id"] != except_id:
                home = _decode_extra((s.get("extra") or {}).get(HOME_FIELD))
                await self.set_location(s["id"], str(home).strip() if home else fallback)


def spool_label(spool: dict) -> str:
    """Kurzer Anzeigename ohne Nummer und Hersteller: 'PLA Matte Dark Brown'."""
    f = spool.get("filament") or {}
    material = (f.get("material") or "").strip()
    name = (f.get("name") or "").strip()
    if material and name.upper().startswith(material.upper()):
        material = ""  # 'PLA' + 'PLA Matte' -> nicht doppelt
    return " ".join(p for p in (material, name) if p) or f"Spule {spool.get('id', '?')}"


def spool_choice_label(spool: dict) -> str:
    """Für Auswahllisten: Name + Hersteller + Rest, damit gleiche Sorten unterscheidbar sind."""
    f = spool.get("filament") or {}
    extra = [(f.get("vendor") or {}).get("name", "")]
    if spool.get("remaining_weight") is not None:
        extra.append(f"{spool['remaining_weight']:.0f} g")
    if spool.get("location"):
        extra.append(spool["location"])
    extra = [e for e in extra if e]
    return spool_label(spool) + (f" ({', '.join(extra)})" if extra else "")


def empty_spool_weight(spool: dict) -> Optional[float]:
    """Leergewicht der Spule: Spule > Filament > Hersteller (wie Spoolman selbst)."""
    f = spool.get("filament") or {}
    for v in (spool.get("spool_weight"), f.get("spool_weight"),
              (f.get("vendor") or {}).get("empty_spool_weight")):
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None
