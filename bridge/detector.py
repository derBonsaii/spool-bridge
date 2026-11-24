"""Slot-Erkennung: verbindet einen NFC-Scan mit dem AMS-Slot, in den die Spule gesteckt wird.

Reine Logik ohne Netzwerk, damit sie sauber testbar ist.

Ablauf A (Normalfall): Scan -> innerhalb `window` Sekunden wird ein Slot belegt -> Zuordnung.
Ablauf B (umgekehrt):  Slot wird belegt -> innerhalb `window` Sekunden Scan  -> Zuordnung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable, Optional

Slot = Hashable  # z.B. (ams_id, slot_index)


@dataclass
class Armed:
    spool_id: int
    expires: float


@dataclass
class SlotDetector:
    window: float = 60.0
    _occupied: Optional[set] = None
    _armed: Optional[Armed] = None
    _recent_inserts: dict = field(default_factory=dict)  # slot -> timestamp

    # ------------------------------------------------------------------ state
    @property
    def initialized(self) -> bool:
        return self._occupied is not None

    @property
    def occupied(self) -> set:
        return set(self._occupied or ())

    @property
    def armed_spool(self) -> Optional[int]:
        return self._armed.spool_id if self._armed else None

    def _expire(self, now: float) -> None:
        if self._armed and now > self._armed.expires:
            self._armed = None
        self._recent_inserts = {
            s: t for s, t in self._recent_inserts.items() if now - t <= self.window
        }

    # ------------------------------------------------------------------ events
    def update_occupancy(self, occupied: set, now: float):
        """Neuen Belegungsstand melden.

        Gibt (assignments, removed) zurück:
          assignments: Liste von (slot, spool_id), die jetzt zugeordnet werden sollen
          removed:     Liste von Slots, die leer geworden sind
        """
        occupied = set(occupied)
        if self._occupied is None:
            # Erster Stand nach dem Start: nur merken, nichts zuordnen.
            self._occupied = occupied
            return [], []

        self._expire(now)
        inserted = sorted(occupied - self._occupied, key=str)
        removed = sorted(self._occupied - occupied, key=str)
        self._occupied = occupied

        for slot in removed:
            self._recent_inserts.pop(slot, None)

        assignments = []
        for slot in inserted:
            if self._armed is not None:
                assignments.append((slot, self._armed.spool_id))
                self._armed = None
            else:
                self._recent_inserts[slot] = now
        return assignments, removed

    def scan(self, spool_id: int, now: float):
        """Ein Tag wurde gescannt. Gibt (slot, spool_id) zurück, falls sofort zuordenbar."""
        self._expire(now)
        if len(self._recent_inserts) == 1:
            # Spule wurde kurz vorher eingesteckt (Ablauf B) – eindeutig.
            slot = next(iter(self._recent_inserts))
            self._recent_inserts.clear()
            self._armed = None
            return slot, spool_id
        # Mehrere frisch eingesteckte Slots wären mehrdeutig -> auf nächsten Insert warten.
        self._recent_inserts.clear()
        self._armed = Armed(spool_id=spool_id, expires=now + self.window)
        return None

    def disarm(self) -> None:
        self._armed = None
