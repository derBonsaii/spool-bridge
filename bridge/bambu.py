"""Lokale MQTT-Verbindung zu einem Bambu-Drucker (LAN-/Developer-Modus nötig)."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .filament import TraySetting

log = logging.getLogger(__name__)

Slot = tuple  # (ams_id, slot_index)


def slot_name(slot: Slot) -> str:
    ams_id, idx = slot
    if ams_id < 4:
        return f"AMS {'ABCD'[ams_id]}{idx + 1}"
    return f"AMS-HT {ams_id - 127}"


class AmsState:
    """Hält den zusammengeführten AMS-Zustand aus (teilweise inkrementellen) Reports."""

    def __init__(self):
        self.units: dict[int, dict[int, dict]] = {}  # ams_id -> slot -> tray-dict
        self.exist_bits: Optional[int] = None
        self.seen = False

    def merge(self, ams_block: dict) -> bool:
        """Report-Block 'print.ams' einarbeiten. True, falls AMS-Daten enthalten waren."""
        changed = False
        if "tray_exist_bits" in ams_block:
            try:
                self.exist_bits = int(str(ams_block["tray_exist_bits"]), 16)
                changed = True
            except ValueError:
                pass
        for unit in ams_block.get("ams") or []:
            try:
                ams_id = int(unit["id"])
            except (KeyError, ValueError):
                continue
            slots = self.units.setdefault(ams_id, {})
            for tray in unit.get("tray") or []:
                try:
                    idx = int(tray["id"])
                except (KeyError, ValueError):
                    continue
                if len(tray) <= 1:
                    slots[idx] = {}  # leerer Slot: nur {"id": "x"}
                else:
                    slots.setdefault(idx, {}).update(tray)
                changed = True
        if changed:
            self.seen = True
        return changed

    def occupied(self) -> set:
        result = set()
        for ams_id, slots in self.units.items():
            for idx, tray in slots.items():
                if ams_id < 4 and self.exist_bits is not None:
                    present = bool(self.exist_bits >> (ams_id * 4 + idx) & 1)
                else:
                    # Fallback (z.B. AMS HT): Slot gilt als belegt, wenn Daten gemeldet werden.
                    present = bool(tray.get("tray_type")) or bool(tray.get("state"))
                if present:
                    result.add((ams_id, idx))
        return result

    def tray(self, slot: Slot) -> dict:
        return self.units.get(slot[0], {}).get(slot[1], {})


class BambuPrinter:
    def __init__(self, name: str, host: str, serial: str, access_code: str,
                 on_update: Callable[["BambuPrinter"], None],
                 loop: asyncio.AbstractEventLoop):
        self.name = name
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.on_update = on_update
        self.loop = loop
        self.state = AmsState()
        self.connected = False
        self.last_ams_reports: list = []  # Diagnose: letzte Roh-Meldungen zum AMS
        self._seq = 0
        self._client: Optional[mqtt.Client] = None

    # ------------------------------------------------------------- Verbindung
    def start(self) -> None:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=f"spool-bridge-{self.name}-{int(time.time())}")
        c.username_pw_set("bblp", self.access_code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Drucker nutzt ein selbstsigniertes Zertifikat
        c.tls_set_context(ctx)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 60)
        c.connect_async(self.host, 8883, keepalive=30)
        c.loop_start()
        self._client = c

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("%s: MQTT-Verbindung abgelehnt (%s) – Access-Code/Developer-Modus prüfen",
                      self.name, reason_code)
            return
        self.connected = True
        log.info("%s: verbunden mit %s", self.name, self.host)
        client.subscribe(f"device/{self.serial}/report")
        self._publish({"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("%s: Verbindung getrennt (%s)", self.name, reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        ams = (data.get("print") or {}).get("ams")
        if isinstance(ams, dict):
            self.last_ams_reports = (self.last_ams_reports + [ams])[-5:]
        if isinstance(ams, dict) and self.state.merge(ams):
            self.loop.call_soon_threadsafe(self.on_update, self)

    # ------------------------------------------------------------- Befehle
    def _next_seq(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _publish(self, payload: dict) -> None:
        if self._client:
            self._client.publish(f"device/{self.serial}/request", json.dumps(payload))

    def set_tray(self, slot: Slot, s: TraySetting) -> None:
        ams_id, idx = slot
        cmd = {
            "print": {
                "sequence_id": self._next_seq(),
                "command": "ams_filament_setting",
                "ams_id": ams_id,
                "tray_id": idx,
                "slot_id": idx,
                "tray_info_idx": s.tray_info_idx,
                "tray_color": s.tray_color,
                "nozzle_temp_min": s.nozzle_temp_min,
                "nozzle_temp_max": s.nozzle_temp_max,
                "tray_type": s.tray_type,
                "setting_id": "",
            }
        }
        log.info("%s: setze %s -> %s %s #%s", self.name, slot_name(slot),
                 s.tray_type, s.tray_info_idx, s.tray_color)
        self._publish(cmd)

    async def verify_tray(self, slot: Slot, s: TraySetting, timeout: float = 15.0) -> bool:
        """Wartet, bis der Drucker die gesetzte Filament-ID für den Slot zurückmeldet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state.tray(slot).get("tray_info_idx") == s.tray_info_idx:
                return True
            await asyncio.sleep(0.5)
        return False
