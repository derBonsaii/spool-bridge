"""Konfiguration aus config.yaml laden."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class Config:
    spoolman_url: str
    printers: dict[str, PrinterCfg]
    readers: dict[str, str]  # reader-id -> printer-name
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
        printers[name] = PrinterCfg(name=name, type=ptype, **p)

    readers = {str(k): str(v) for k, v in (raw.get("readers") or {}).items()}
    for reader, printer in readers.items():
        if printer not in printers:
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
