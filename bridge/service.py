"""Kernlogik: Scans entgegennehmen, Slots erkennen, Drucker und Spoolman aktualisieren."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

from . import __version__
from .bambu import BambuPrinter, Slot, slot_name
from .config import Config
from .detector import SlotDetector
from .filament import bambu_spool_info, tray_setting
from .moonraker import Moonraker
from .spoolman import Spoolman, empty_spool_weight, filament_label, spool_tags, uid_matches, normalize_uid, spool_choice_label, spool_label

log = logging.getLogger(__name__)


def spool_view(spool: dict) -> dict:
    """Spule fürs Web-UI."""
    f = spool.get("filament") or {}
    color = (f.get("color_hex") or "")
    if not color and f.get("multi_color_hexes"):
        color = f["multi_color_hexes"].split(",")[0]
    return {
        "id": spool["id"],
        "label": spool_label(spool),
        "vendor": (f.get("vendor") or {}).get("name", "") or "",
        "color": color.lstrip("#")[:6],
        "remaining": spool.get("remaining_weight"),
        "total": spool.get("initial_weight") or f.get("weight"),
        "empty": empty_spool_weight(spool),
        "location": spool.get("location") or "",
        "tags": len(spool_tags(spool)),
    }


class ScaleState:
    """Live-Zustand einer Waage (wird laufend von der Waage gemeldet)."""

    STABLE_COUNT = 3        # so viele Meldungen in Folge ...
    STABLE_RANGE = 2.0      # ... innerhalb dieser Spanne (g) = Gewicht ruhig
    MIN_WEIGHT = 30.0       # darunter gilt der Teller als leer
    REAPPLY_DIFF = 2.0      # erst bei dieser Änderung (g) erneut in Spoolman schreiben

    def __init__(self, reader: str):
        self.reader = reader
        self.weight: Optional[float] = None
        self.uid = ""
        self.t: Optional[float] = None
        self.history: deque = deque(maxlen=self.STABLE_COUNT)
        self.last_applied: dict[str, float] = {}
        self.spool_id: Optional[int] = None    # zuletzt erkannte Spule auf der Waage
        self.spool_seen: Optional[float] = None
        self.cmd: Optional[str] = None          # wartender Befehl an die Waage

    def stable_weight(self) -> Optional[float]:
        if len(self.history) < self.STABLE_COUNT:
            return None
        if max(self.history) - min(self.history) > self.STABLE_RANGE:
            return None
        return sum(self.history) / len(self.history)


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
        self._spool_cache: tuple = (None, [])
        self._filament_cache: tuple = (None, [])
        self._rfid: dict[str, dict] = {}  # printer -> slot -> {uuid, spool_id, remain}
        self.scales: dict[str, ScaleState] = {}

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
        pc = self.cfg.printers[printer_name]
        if slot is None:
            return pc.location
        ams_id, idx = slot
        if ams_id >= 4:  # AMS HT: eigenes, einfaches Format
            return f"{pc.location} {slot_name(slot)}"
        fmt = pc.slot_location or "{printer} AMS {unit}{slot}"
        return fmt.format(printer=pc.location, unit="ABCD"[ams_id], slot=idx + 1)

    def _bambuddy(self, printer_name: str) -> bool:
        return self.cfg.printers[printer_name].bambu_rfid == "bambuddy"

    def _is_printer_location(self, location: str) -> bool:
        for p in self.cfg.printers.values():
            if location == p.location or location.startswith(p.location + " "):
                return True
        return False

    async def cached_spools(self, max_age: float = 5.0) -> list:
        ts, spools = self._spool_cache
        if ts is None or self.clock() - ts > max_age:
            spools = await self.spoolman.spools()
            self._spool_cache = (self.clock(), spools)
        return spools

    async def cached_filaments(self, max_age: float = 30.0) -> list:
        ts, fil = self._filament_cache
        if ts is None or self.clock() - ts > max_age:
            fil = await self.spoolman.filaments()
            self._filament_cache = (self.clock(), fil)
        return fil

    def _invalidate(self) -> None:
        self._spool_cache = (None, [])
        self._filament_cache = (None, [])

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
        scale = self.scales.setdefault(reader, ScaleState(reader))
        scale.spool_id, scale.spool_seen = spool["id"], self.clock()
        if weight is not None:
            remaining = await self._apply_weight(spool, weight)
            scale.last_applied[uid] = weight
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

    async def link_tag(self, uid: str, spool_id: Optional[int] = None,
                       filament_id: Optional[int] = None) -> dict:
        if spool_id is None:
            if filament_id is None:
                raise ValueError("spool_id oder filament_id nötig")
            spool = await self.spoolman.create_spool(filament_id, self.cfg.storage_location)
            spool_id = spool["id"]
            self.event("ok", f"Neue Spule {spool_label(spool)} angelegt")
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
    async def handle_weight(self, reader: str, uid: str, weight: Optional[float]) -> dict:
        """Laufende Meldung einer Waage: Gewicht + Tag, der gerade aufliegt.

        Liegt eine bekannte Spule ruhig auf und hat sich das Gewicht seit dem letzten
        Eintrag geändert, wird das Restgewicht in Spoolman nachgeführt. Die Antwort
        enthält einen wartenden Befehl (Tara/Kalibrieren) für die Waage.
        """
        if reader not in self.cfg.readers:
            return {"cmd": ""}
        s = self.scales.setdefault(reader, ScaleState(reader))
        uid = normalize_uid(uid)
        if s.uid and not uid:
            s.last_applied.clear()  # Spule abgehoben -> beim nächsten Auflegen neu wiegen
        s.uid, s.t = uid, self.clock()
        if weight is None or weight < ScaleState.MIN_WEIGHT:
            s.weight = weight
            s.history.clear()
        else:
            s.weight = weight
            s.history.append(weight)

        stable = s.stable_weight()
        if uid and stable is not None:
            last = s.last_applied.get(uid)
            if last is None or abs(stable - last) > ScaleState.REAPPLY_DIFF:
                spool = await self.spoolman.find_by_uid(uid)
                if spool is not None:
                    s.last_applied[uid] = stable
                    s.spool_id, s.spool_seen = spool["id"], self.clock()
                    await self._apply_weight(spool, stable)
        if uid and s.spool_id is not None:
            s.spool_seen = self.clock()

        cmd, s.cmd = s.cmd, None
        return {"cmd": cmd or ""}

    def scale_command(self, reader: str, cmd: str, grams: Optional[float] = None) -> None:
        s = self.scales.setdefault(reader, ScaleState(reader))
        if cmd == "tare":
            s.cmd = "tare"
            self.event("info", f"Waage {reader}: Tara angefordert")
        elif cmd == "calibrate" and grams and grams > 0:
            s.cmd = f"cal:{grams:.1f}"
            self.event("info", f"Waage {reader}: Kalibrieren mit {grams:.0f} g angefordert")
        else:
            raise ValueError("unbekannter Befehl")

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
        await self.spoolman.place(spool, loc, self._is_printer_location)
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
                if self._bambuddy(printer.name):
                    self._spawn(self._learn_bambu_tag(printer.name, slot, info))
                else:
                    self._spawn(self._register_bambu_spool(printer.name, slot, info))
            elif self._bambuddy(printer.name):
                continue  # Restmenge führt Bambuddy nach
            elif known["spool_id"] and info["remain_pct"] != known["remain"] and info["remaining"] is not None:
                known["remain"] = info["remain_pct"]
                self._spawn(self._sync_remaining(known["spool_id"], info["remaining"]))

    async def _learn_bambu_tag(self, printer: str, slot: Slot, info: dict) -> None:
        """Bambuddy-Modus: nur die RFID-Kennung an die (von Bambuddy angelegte) Spule hängen."""
        if not info.get("tag_uid"):
            return
        spool = None
        for _ in range(6):  # Bambuddy legt die Spule evtl. erst gerade an
            spool = await self.spoolman.find_by_bambu_uuid(info["uuid"])
            if spool:
                break
            await asyncio.sleep(5)
        if spool is None:
            log.info("%s %s: Bambu-Spule (noch) nicht in Spoolman – Bambuddy-Sync prüfen",
                     printer, slot_name(slot))
            self._rfid.get(printer, {}).pop(slot, None)  # beim nächsten Report erneut
            return
        if not any(uid_matches(t, info["tag_uid"]) for t in spool_tags(spool)):
            await self.spoolman.link_uid(spool["id"], info["tag_uid"])
            self._invalidate()
            self.event("info", f"{printer} {slot_name(slot)}: {spool_label(spool)} – RFID-Tag für "
                               f"Leser/Waage gemerkt")

    async def _register_bambu_spool(self, printer: str, slot: Slot, info: dict) -> None:
        try:
            await self._register_bambu_spool_inner(printer, slot, info)
        except Exception:
            # beim nächsten Report erneut versuchen
            self._rfid.get(printer, {}).pop(slot, None)
            raise

    async def _register_bambu_spool_inner(self, printer: str, slot: Slot, info: dict) -> None:
        loc = self._location(printer, slot)
        spool, how = await self.spoolman.find_bambu_candidate(info, loc)
        if spool is None:
            filament = await self.spoolman.find_or_create_bambu_filament(info)
            spool = await self.spoolman.create_bambu_spool(filament["id"], info["uuid"], loc,
                                                           info["remaining"])
            hint = (" – mehrere passende Spulen ohne Bambu-UUID gefunden, bitte Duplikat prüfen"
                    if how == "ambiguous" else "")
            self.event("warn" if hint else "ok",
                       f"{printer} {slot_name(slot)}: Bambu-Spule erkannt und in Spoolman "
                       f"angelegt ({spool_label(spool)}){hint}")
        elif how != "uuid":
            # z.B. über die Waage angelegt: UUID nachtragen, Restgewicht der Waage behalten
            await self.spoolman.adopt_bambu_spool(spool, info["uuid"])
            self.event("ok", f"{printer} {slot_name(slot)}: Bambu-Spule {spool_label(spool)} "
                             f"erkannt und mit der bestehenden Spule verbunden")
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
        await self.spoolman.place(spool, loc, self._is_printer_location)
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
        await self.spoolman.move_all(loc, self.cfg.storage_location, except_id=spool["id"],
                                     skip_bambu=self._bambuddy(printer))
        await self.spoolman.place(spool, loc, self._is_printer_location)
        self._invalidate()
        ok = await p.verify_tray(slot, setting)
        if ok:
            self.event("ok", f"{printer} {slot_name(slot)}: {spool_label(spool)} eingestellt")
        else:
            self.event("warn", f"{printer} {slot_name(slot)}: Befehl gesendet, aber keine Bestätigung "
                               f"vom Drucker – Slot am Display prüfen")

    async def _slot_emptied(self, printer: str, slot: Slot) -> None:
        loc = self._location(printer, slot)
        await self.spoolman.move_all(loc, self.cfg.storage_location,
                                     skip_bambu=self._bambuddy(printer))
        self._invalidate()
        self.event("info", f"{printer} {slot_name(slot)}: Spule entnommen")

    # ------------------------------------------------------------ Status fürs UI
    async def state(self) -> dict:
        try:
            spools = await self.cached_spools()
            filaments = await self.cached_filaments() if self.unknown else []
        except Exception as e:  # Spoolman nicht erreichbar
            spools, filaments = [], []
            log.warning("Spoolman nicht erreichbar: %s", e)
        by_loc = {s.get("location"): spool_label(s) for s in spools if s.get("location")}
        by_id = {s["id"]: spool_label(s) for s in spools}

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
                             "armed": armed, "armed_label": by_id.get(armed, "") if armed else "",
                             "slots": slots})
        for name in self.moonraker:
            printers.append({"name": name, "type": "moonraker", "connected": True,
                             "spool": by_loc.get(self._location(name), "")})
        now = self.clock()
        spool_by_id = {sp["id"]: sp for sp in spools}
        scales = []
        for r, sc in self.scales.items():
            spool = None
            if sc.spool_id is not None and sc.spool_seen is not None and now - sc.spool_seen < 30:
                spool = spool_by_id.get(sc.spool_id)
            scales.append({
                "reader": r,
                "printer": self.cfg.readers.get(r) or "",
                "online": sc.t is not None and now - sc.t < 8,
                "weight": sc.weight,
                "uid": sc.uid,
                "pending": sc.cmd or "",
                "spool": spool_view(spool) if spool else None,
                "unknown_tag": bool(sc.uid) and (self.unknown or {}).get("uid") == sc.uid,
            })
        return {
            "version": __version__,
            "printers": printers,
            "scales": scales,
            "inventory": sorted((spool_view(sp) for sp in spools),
                                key=lambda x: (x["location"].lower(), x["label"].lower())),
            "events": list(self.events),
            "unknown": self.unknown,
            "filaments": sorted(({"id": f["id"], "label": filament_label(f)} for f in filaments),
                                key=lambda x: x["label"].lower()),
            "spools": sorted(({"id": s["id"], "label": spool_choice_label(s)} for s in spools),
                             key=lambda x: x["label"].lower()),
        }
