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
from .filament import tray_setting
from .moonraker import Moonraker
from .spoolman import Spoolman, normalize_uid, spool_label

log = logging.getLogger(__name__)


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
    async def handle_scan(self, reader: str, uid: str) -> dict:
        uid = normalize_uid(uid)
        printer = self.cfg.readers.get(reader)
        if not printer:
            self.event("error", f"Unbekannter Leser '{reader}' – in config.yaml unter readers eintragen")
            return {"result": "error", "message": "unknown reader"}

        spool = await self.spoolman.find_by_uid(uid)
        if spool is None:
            self.unknown = {"uid": uid, "reader": reader, "t": time.time()}
            self.event("warn", f"Unbekannter Tag {uid} – im Web-UI einer Spule zuweisen")
            return {"result": "unknown", "uid": uid}

        label = spool_label(spool)
        if printer in self.moonraker:
            await self._assign_moonraker(printer, spool)
            return {"result": "assigned", "spool": label}

        det = self.detectors[printer]
        hit = det.scan(spool["id"], self.clock())
        if hit:
            slot, _ = hit
            await self.assign_bambu(printer, slot, spool)
            return {"result": "assigned", "spool": label, "slot": slot_name(slot)}
        self.event("info", f"{label} bereit – jetzt in einen AMS-Slot am {printer} stecken "
                           f"({int(self.cfg.window_seconds)} s)")
        return {"result": "armed", "spool": label}

    async def link_tag(self, uid: str, spool_id: int) -> dict:
        await self.spoolman.link_uid(spool_id, uid)
        self._invalidate()
        spool = await self.spoolman.spool(spool_id)
        self.event("ok", f"Tag {normalize_uid(uid)} mit {spool_label(spool)} verknüpft")
        reader = (self.unknown or {}).get("reader")
        self.unknown = None
        if reader:  # gleich so behandeln, als wäre die Spule gescannt worden
            return await self.handle_scan(reader, uid)
        return {"result": "linked"}

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
        assignments, removed = det.update_occupancy(st.occupied(), self.clock())
        for slot, spool_id in assignments:
            self._spawn(self._assign_by_id(printer.name, slot, spool_id))
        for slot in removed:
            self._spawn(self._slot_emptied(printer.name, slot))

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
