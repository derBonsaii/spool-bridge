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


DENSITY = {
    "PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
    "PA": 1.14, "PC": 1.20, "PVA": 1.23, "HIPS": 1.04,
}

EMPTY_UUID = "0" * 32


def bambu_spool_info(tray: dict):
    """Liest aus einem AMS-Tray die Daten einer Original-Bambu-Spule (RFID).

    Gibt None zurück, wenn im Slot keine Spule mit gültiger Bambu-RFID steckt.
    """
    uuid = str(tray.get("tray_uuid") or "").upper()
    if len(uuid) != 32 or uuid == EMPTY_UUID:
        return None
    material = (tray.get("tray_type") or "PLA").strip()
    base = base_material(material)

    def num(key, default=None):
        try:
            v = float(tray.get(key))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    weight = num("tray_weight", 1000.0)
    remain = tray.get("remain")
    try:
        remain = int(remain)
    except (TypeError, ValueError):
        remain = -1
    tmin, tmax = num("nozzle_temp_min"), num("nozzle_temp_max")
    return {
        "uuid": uuid,
        "name": (tray.get("tray_sub_brands") or material).strip(),
        "material": material,
        "color_hex": str(tray.get("tray_color") or "FFFFFFFF")[:6].upper(),
        "idx": str(tray.get("tray_info_idx") or ""),
        "weight": weight,
        "diameter": num("tray_diameter", 1.75),
        "density": DENSITY.get(base, 1.24),
        "extruder_temp": int((tmin + tmax) / 2) if tmin and tmax else None,
        "bed_temp": int(num("bed_temp")) if num("bed_temp") else None,
        "remaining": weight * remain / 100 if 0 <= remain <= 100 else None,
        "remain_pct": remain,
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
