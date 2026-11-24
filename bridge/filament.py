"""Übersetzt eine Spoolman-Spule in AMS-Slot-Einstellungen für Bambu-Drucker."""

from __future__ import annotations

from dataclasses import dataclass

from .spoolman import BAMBU_IDX_FIELD, _decode_extra

# Generische Bambu-Filament-Presets ("Generic PLA" usw.).
# Pro Filament in Spoolman überschreibbar über das Extra-Feld "Bambu Filament-ID".
GENERIC_IDX = {
    "PLA": "GFL99",
    "PETG": "GFG99",
    "ABS": "GFB99",
    "ASA": "GFB98",
    "TPU": "GFU99",
    "PA": "GFN99",
    "PC": "GFC99",
    "PVA": "GFS99",
    "HIPS": "GFS98",
}

# Fallback-Düsentemperaturen (min, max), falls in Spoolman keine Temperatur gesetzt ist.
DEFAULT_TEMPS = {
    "PLA": (190, 230),
    "PETG": (230, 260),
    "ABS": (240, 270),
    "ASA": (240, 270),
    "TPU": (200, 240),
    "PA": (260, 290),
    "PC": (260, 290),
    "PVA": (190, 220),
    "HIPS": (220, 250),
}


@dataclass
class TraySetting:
    tray_type: str
    tray_info_idx: str
    tray_color: str  # RRGGBBAA
    nozzle_temp_min: int
    nozzle_temp_max: int


def base_material(material: str) -> str:
    """'PETG-CF' -> 'PETG', 'PLA Matte' -> 'PLA', 'ASA+' -> 'ASA'."""
    m = (material or "PLA").upper()
    for key in sorted(GENERIC_IDX, key=len, reverse=True):
        if m.startswith(key):
            return key
    return m.split()[0] if m.split() else "PLA"


def tray_setting(spool: dict) -> TraySetting:
    f = spool.get("filament") or {}
    material = (f.get("material") or "PLA").strip()
    base = base_material(material)

    idx = _decode_extra((f.get("extra") or {}).get(BAMBU_IDX_FIELD))
    idx = (idx or "").strip() or GENERIC_IDX.get(base, "GFL99")

    color = (f.get("color_hex") or "")
    if not color and f.get("multi_color_hexes"):
        color = f["multi_color_hexes"].split(",")[0]
    color = color.lstrip("#").upper()[:6] or "FFFFFF"

    temp = f.get("settings_extruder_temp")
    if temp:
        tmin, tmax = int(temp) - 20, int(temp) + 10
    else:
        tmin, tmax = DEFAULT_TEMPS.get(base, (190, 240))

    return TraySetting(
        tray_type=base,
        tray_info_idx=idx,
        tray_color=color + "FF",
        nozzle_temp_min=tmin,
        nozzle_temp_max=tmax,
    )
