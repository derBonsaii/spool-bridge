"""Konfiguration aus config.yaml laden."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

import yaml


@dataclass
class PrinterCfg:
    name: str
    type: str  # "bambu" | "moonraker"
    host: str = ""
    serial: str = ""
    access_code: str = ""
    url: str = ""
    location: str = ""  # Lagerort-Präfix in Spoolman, z.B. "X2D"
    # Wer kümmert sich um Original-Bambu-Spulen (RFID) in Spoolman?
    #   "bridge"   – die Bridge legt sie an, setzt Ort und Restmenge
    #   "bambuddy" – Bambuddy macht das; die Bridge merkt sich nur die Tag-Kennung
    bambu_rfid: str = "bridge"
    # Format des Lagerorts für AMS-Slots. Platzhalter: {printer} {unit} {slot}
    # Standard: "X2D AMS A1". Bambuddy-Format: "{printer} - AMS {unit} Slot {slot}"
    slot_location: str = ""


@dataclass
class Config:
    spoolman_url: str
    printers: dict[str, PrinterCfg]
    readers: dict[str, Optional[str]]  # reader-id -> printer-name (None = nur Waage)
    storage_location: str = "Lager"
    window_seconds: float = 60.0
    port: int = 8099
    extra: dict = field(default_factory=dict)


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    printers = {}
    for name, p in (raw.get("printers") or {}).items():
        p = dict(p or {})
        ptype = p.pop("type", "")
        if ptype not in ("bambu", "moonraker"):
            raise ValueError(f"Drucker '{name}': type muss 'bambu' oder 'moonraker' sein")
        p.setdefault("location", name.upper())
        if p.get("bambu_rfid", "bridge") not in ("bridge", "bambuddy"):
            raise ValueError(f"Drucker '{name}': bambu_rfid muss 'bridge' oder 'bambuddy' sein")
        if not p.get("slot_location"):
            p["slot_location"] = ("{printer} - AMS {unit} Slot {slot}"
                                  if p.get("bambu_rfid") == "bambuddy" else "{printer} AMS {unit}{slot}")
        printers[name] = PrinterCfg(name=name, type=ptype, **p)

    readers = {str(k): (str(v) if v else None) for k, v in (raw.get("readers") or {}).items()}
    for reader, printer in readers.items():
        if printer is not None and printer not in printers:
            raise ValueError(f"Leser '{reader}' verweist auf unbekannten Drucker '{printer}'")

    if not raw.get("spoolman_url"):
        raise ValueError("spoolman_url fehlt in der Konfiguration")

    return Config(
        spoolman_url=raw["spoolman_url"],
        printers=printers,
        readers=readers,
        storage_location=raw.get("storage_location", "Lager"),
        window_seconds=float(raw.get("window_seconds", 60)),
        port=int(raw.get("port", 8099)),
    )
