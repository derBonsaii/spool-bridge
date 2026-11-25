"""Kernlogik: Scans entgegennehmen, Slots erkennen, Drucker und Spoolman aktualisieren."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

from .bambu import BambuPrinter, Slot, slot_name
from .config import Config
from .detector import SlotDetector
from .filament import bambu_spool_info, tray_setting
from .moonraker import Moonraker
from .spoolman import Spoolman, empty_spool_weight, normalize_uid, spool_label

log = logging.getLogger(__name__)


def display_fields(spool: dict, remaining: Optional[float] = None) -> dict:
    """Kurze Texte fürs Display am Leser (alles als Strings)."""
    f = spool.get("filament") or {}
    if remaining is None and spool.get("remaining_weight") is not None:
        remaining = spool["remaining_weight"]
    return {
        "vendor": (f.get("vendor") or {}).get("name", "") or "",
        "material": f.get("material") or "",
        "name": f.get("name") or "",
        "remaining": f"{remaining:.0f}" if remaining is not None else "",
    }


class Bridge:
    def __init__(self, cfg: Config, spoolman: Spoolman,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.spoolman = spoolman
        self.clock = clock
        self.bambu: dict[str, BambuPrinter] = {}
        self.moonraker: dict[str, Moonraker] = {}
        self.detectors: dict[str, SlotDetector] = {}
        self.events: deque = deque(maxlen=50)
        self.unknown: Optional[dict] = None
        self._tasks: set = set()
        self._spool_cache: tuple[float, list] = (0.0, [])
        self._rfid: dict[str, dict] = {}  # printer -> slot -> {uuid, spool_id, remain}

    # ------------------------------------------------------------ Setup
    def add_bambu(self, printer: BambuPrinter) -> None:
        self.bambu[printer.name] = printer
        self.detectors[printer.name] = SlotDetector(window=self.cfg.window_seconds)

    def add_moonraker(self, mr: Moonraker) -> None:
        self.moonraker[mr.name] = mr

    # ------------------------------------------------------------ Hilfen
    def event(self, level: str, text: str) -> None:
        log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, text)
        self.events.appendleft({"t": time.time(), "level": level, "text": text})

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            self.event("error", f"Fehler: {task.exception()}")

    def _location(self, printer_name: str, slot: Optional[Slot] = None) -> str:
        base = self.cfg.printers[printer_name].location
        return f"{base} {slot_name(slot)}" if slot is not None else base

    async def cached_spools(self, max_age: float = 5.0) -> list:
        ts, spools = self._spool_cache
        if self.clock() - ts > max_age:
            spools = await self.spoolman.spools()
            self._spool_cache = (self.clock(), spools)
        return spools

    def _invalidate(self) -> None:
        self._spool_cache = (0.0, [])

    # ------------------------------------------------------------ Scan
    async def handle_scan(self, reader: str, uid: str, weight: Optional[float] = None) -> dict:
        uid = normalize_uid(uid)
        if reader not in self.cfg.readers:
            self.event("error", f"Unbekannter Leser '{reader}' – in config.yaml unter readers eintragen")
            return {"result": "error", "message": "unknown reader"}
        printer = self.cfg.readers[reader]  # None = reine Waage ohne Drucker

        spool = await self.spoolman.find_by_uid(uid)
        if spool is None:
            self.unknown = {"uid": uid, "reader": reader, "weight": weight, "t": time.time()}
            self.event("warn", f"Unbekannter Tag {uid} – im Web-UI einer Spule zuweisen")
            return {"result": "unknown", "uid": uid}

        label = spool_label(spool)
        remaining = None
        if weight is not None:
            remaining = await self._apply_weight(spool, weight)
        info = display_fields(spool, remaining)
        if not printer:
            return {"result": "weighed", "spool": label, **info}
        if printer in self.moonraker:
            await self._assign_moonraker(printer, spool)
            return {"result": "assigned", "spool": label, **info}

        det = self.detectors[printer]
        # Slots mit Original-Bambu-Spule (RFID) nie einem gescannten Tag zuordnen
        rfid_slots = {s for s in det.occupied
                      if bambu_spool_info(self.bambu[printer].state.tray(s))}
        hit = det.scan(spool["id"], self.clock(), exclude=rfid_slots)
        if hit:
            slot, _ = hit
            await self.assign_bambu(printer, slot, spool)
            return {"result": "assigned", "spool": label, "slot": slot_name(slot), **info}
        self.event("info", f"{label} bereit – jetzt in einen AMS-Slot am {printer} stecken "
                           f"({int(self.cfg.window_seconds)} s)")
        return {"result": "armed", "spool": label, **info}

    async def link_tag(self, uid: str, spool_id: int) -> dict:
        await self.spoolman.link_uid(spool_id, uid)
        self._invalidate()
        spool = await self.spoolman.spool(spool_id)
        self.event("ok", f"Tag {normalize_uid(uid)} mit {spool_label(spool)} verknüpft")
        reader = (self.unknown or {}).get("reader")
        weight = (self.unknown or {}).get("weight")
        self.unknown = None
        if reader:  # gleich so behandeln, als wäre die Spule gescannt worden
            return await self.handle_scan(reader, uid, weight)
        return {"result": "linked"}

    # ------------------------------------------------------------ Waage
    async def _apply_weight(self, spool: dict, gross: float) -> Optional[float]:
        """Bruttogewicht (Spule auf der Waage) -> Restgewicht in Spoolman."""
        empty = empty_spool_weight(spool)
        label = spool_label(spool)
        if empty is None:
            self.event("warn", f"{label}: {gross:.0f} g gewogen, aber kein Leergewicht der Spule "
                               f"in Spoolman – bei Spule, Filament oder Hersteller eintragen")
            return None
        remaining = max(0.0, gross - empty)
        await self.spoolman.set_remaining(spool["id"], remaining)
        self._invalidate()
        self.event("ok", f"{label}: {gross:.0f} g gewogen − {empty:.0f} g Leerspule "
                         f"= {remaining:.0f} g Rest")
        return remaining

    # ------------------------------------------------------------ Moonraker
    async def _assign_moonraker(self, printer: str, spool: dict) -> None:
        await self.moonraker[printer].set_active_spool(spool["id"])
        loc = self._location(printer)
        await self.spoolman.move_all(loc, self.cfg.storage_location, except_id=spool["id"])
        await self.spoolman.set_location(spool["id"], loc)
        self._invalidate()
        self.event("ok", f"{printer}: aktive Spule {spool_label(spool)}")

    # ------------------------------------------------------------ Bambu
    def on_bambu_update(self, printer: BambuPrinter) -> None:
        st = printer.state
        if any(a < 4 for a in st.units) and st.exist_bits is None:
            return  # noch kein vollständiger Stand – Basis erst nach pushall bilden
        det = self.detectors[printer.name]
        occupied = st.occupied()
        assignments, removed = det.update_occupancy(occupied, self.clock())
        seen = self._rfid.setdefault(printer.name, {})
        for slot, spool_id in assignments:
            self._spawn(self._assign_by_id(printer.name, slot, spool_id))
        for slot in removed:
            seen.pop(slot, None)
            self._spawn(self._slot_emptied(printer.name, slot))

        # Original-Bambu-Spulen (RFID): automatisch in Spoolman anlegen bzw. nachführen
        for slot in occupied:
            info = bambu_spool_info(st.tray(slot))
            if info is None:
                continue
            known = seen.get(slot)
            if known is None or known["uuid"] != info["uuid"]:
                seen[slot] = {"uuid": info["uuid"], "spool_id": None, "remain": info["remain_pct"]}
                self._spawn(self._register_bambu_spool(printer.name, slot, info))
            elif known["spool_id"] and info["remain_pct"] != known["remain"] and info["remaining"] is not None:
                known["remain"] = info["remain_pct"]
                self._spawn(self._sync_remaining(known["spool_id"], info["remaining"]))

    async def _register_bambu_spool(self, printer: str, slot: Slot, info: dict) -> None:
        try:
            await self._register_bambu_spool_inner(printer, slot, info)
        except Exception:
            # beim nächsten Report erneut versuchen
            self._rfid.get(printer, {}).pop(slot, None)
            raise

    async def _register_bambu_spool_inner(self, printer: str, slot: Slot, info: dict) -> None:
        loc = self._location(printer, slot)
        spool = await self.spoolman.find_by_bambu_uuid(info["uuid"])
        if spool is None:
            filament = await self.spoolman.find_or_create_bambu_filament(info)
            spool = await self.spoolman.create_bambu_spool(filament["id"], info["uuid"], loc,
                                                           info["remaining"])
            self.event("ok", f"{printer} {slot_name(slot)}: Bambu-Spule erkannt und in Spoolman "
                             f"angelegt ({spool_label(spool)})")
        else:
            if spool.get("archived"):
                await self.spoolman._req("PATCH", f"/spool/{spool['id']}", json={"archived": False})
            if info["remaining"] is not None:
                await self.spoolman.set_remaining(spool["id"], info["remaining"])
            self.event("ok", f"{printer} {slot_name(slot)}: Bambu-Spule {spool_label(spool)} erkannt")
        if info.get("tag_uid"):
            # RFID-Kennung des gelesenen Chips merken -> Spule ist am Leser/an der Waage erkennbar
            await self.spoolman.link_uid(spool["id"], info["tag_uid"])
        await self.spoolman.move_all(loc, self.cfg.storage_location, except_id=spool["id"])
        await self.spoolman.set_location(spool["id"], loc)
        self._invalidate()
        entry = self._rfid.get(printer, {}).get(slot)
        if entry and entry["uuid"] == info["uuid"]:
            entry["spool_id"] = spool["id"]

    async def _sync_remaining(self, spool_id: int, grams: float) -> None:
        await self.spoolman.set_remaining(spool_id, grams)
        self._invalidate()

    async def _assign_by_id(self, printer: str, slot: Slot, spool_id: int) -> None:
        await self.assign_bambu(printer, slot, await self.spoolman.spool(spool_id))

    async def assign_bambu(self, printer: str, slot: Slot, spool: dict) -> None:
        p = self.bambu[printer]
        setting = tray_setting(spool)
        p.set_tray(slot, setting)
        loc = self._location(printer, slot)
        await self.spoolman.move_all(loc, self.cfg.storage_location, except_id=spool["id"])
        await self.spoolman.set_location(spool["id"], loc)
        self._invalidate()
        ok = await p.verify_tray(slot, setting)
        if ok:
            self.event("ok", f"{printer} {slot_name(slot)}: {spool_label(spool)} eingestellt")
        else:
            self.event("warn", f"{printer} {slot_name(slot)}: Befehl gesendet, aber keine Bestätigung "
                               f"vom Drucker – Slot am Display prüfen")

    async def _slot_emptied(self, printer: str, slot: Slot) -> None:
        loc = self._location(printer, slot)
        await self.spoolman.move_all(loc, self.cfg.storage_location)
        self._invalidate()
        self.event("info", f"{printer} {slot_name(slot)}: Spule entnommen")

    # ------------------------------------------------------------ Status fürs UI
    async def state(self) -> dict:
        try:
            spools = await self.cached_spools()
        except Exception as e:  # Spoolman nicht erreichbar
            spools = []
            log.warning("Spoolman nicht erreichbar: %s", e)
        by_loc = {s.get("location"): spool_label(s) for s in spools if s.get("location")}

        printers = []
        for name, p in self.bambu.items():
            det = self.detectors[name]
            occ = p.state.occupied()
            slots = []
            for ams_id in sorted(p.state.units):
                for idx in sorted(p.state.units[ams_id]):
                    slot = (ams_id, idx)
                    tray = p.state.tray(slot)
                    slots.append({
                        "ams_id": ams_id, "slot": idx, "name": slot_name(slot),
                        "occupied": slot in occ,
                        "type": tray.get("tray_type", ""),
                        "color": (tray.get("tray_color") or "")[:6],
                        "spool": by_loc.get(self._location(name, slot), ""),
                    })
            armed = det.armed_spool
            printers.append({"name": name, "type": "bambu", "connected": p.connected,
                             "armed": armed, "slots": slots})
        for name in self.moonraker:
            printers.append({"name": name, "type": "moonraker", "connected": True,
                             "spool": by_loc.get(self._location(name), "")})
        return {
            "printers": printers,
            "events": list(self.events),
            "unknown": self.unknown,
            "spools": [{"id": s["id"], "label": spool_label(s)} for s in spools],
        }
