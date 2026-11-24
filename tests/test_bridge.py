import asyncio
import json

import pytest
from aiohttp import ClientSession, web

from bridge.bambu import AmsState, BambuPrinter
from bridge.config import Config, PrinterCfg
from bridge.detector import SlotDetector
from bridge.filament import base_material, tray_setting
from bridge.service import Bridge
from bridge.spoolman import Spoolman, normalize_uid


# ---------------------------------------------------------------- Detector
def test_scan_then_insert():
    d = SlotDetector(window=60)
    d.update_occupancy({(0, 0)}, now=0)
    assert d.scan(7, now=1) is None
    assert d.armed_spool == 7
    assigned, removed = d.update_occupancy({(0, 0), (0, 2)}, now=5)
    assert assigned == [((0, 2), 7)] and removed == []
    assert d.armed_spool is None


def test_insert_then_scan():
    d = SlotDetector(window=60)
    d.update_occupancy(set(), now=0)
    assert d.update_occupancy({(0, 3)}, now=1) == ([], [])
    assert d.scan(9, now=10) == ((0, 3), 9)


def test_window_expires():
    d = SlotDetector(window=30)
    d.update_occupancy(set(), now=0)
    d.scan(1, now=0)
    assert d.update_occupancy({(0, 1)}, now=31) == ([], [])


def test_first_report_is_baseline_only():
    d = SlotDetector()
    d.scan(1, now=0)
    assert d.update_occupancy({(0, 0), (0, 1)}, now=1) == ([], [])


def test_two_recent_inserts_are_ambiguous():
    d = SlotDetector()
    d.update_occupancy(set(), now=0)
    d.update_occupancy({(0, 0), (0, 1)}, now=1)
    assert d.scan(4, now=2) is None  # nicht raten, auf nächsten Insert warten
    assert d.update_occupancy({(0, 0), (0, 1), (0, 2)}, now=3)[0] == [((0, 2), 4)]


def test_removed():
    d = SlotDetector()
    d.update_occupancy({(0, 0), (1, 1)}, now=0)
    assert d.update_occupancy({(0, 0)}, now=1) == ([], [(1, 1)])


# ---------------------------------------------------------------- AMS-State
def test_ams_state_exist_bits_and_incremental():
    s = AmsState()
    s.merge({"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}, {"id": "1"},
                                          {"id": "2"}, {"id": "3"}]}],
             "tray_exist_bits": "1"})
    assert s.occupied() == {(0, 0)}
    s.merge({"tray_exist_bits": "5"})  # Slot 3 (Index 2) belegt, ohne Tray-Daten
    assert s.occupied() == {(0, 0), (0, 2)}
    s.merge({"ams": [{"id": "1", "tray": [{"id": str(i)} for i in range(4)]}], "tray_exist_bits": "25"})
    assert s.occupied() == {(0, 0), (0, 2), (1, 1)}


# ---------------------------------------------------------------- Filament
def test_tray_setting_defaults_and_override():
    spool = {"id": 1, "filament": {"material": "PETG-CF", "color_hex": "#ff8800",
                                   "settings_extruder_temp": 250}}
    t = tray_setting(spool)
    assert (t.tray_type, t.tray_info_idx, t.tray_color) == ("PETG", "GFG99", "FF8800FF")
    assert (t.nozzle_temp_min, t.nozzle_temp_max) == (230, 260)

    spool["filament"]["extra"] = {"bambu_idx": json.dumps("GFG98")}
    assert tray_setting(spool).tray_info_idx == "GFG98"
    assert base_material("ASA+") == "ASA"
    assert base_material("PLA Matte") == "PLA"
    assert normalize_uid("04-a1:b2 C3") == "04A1B2C3"


# ---------------------------------------------------------------- End-to-End
class FakeSpoolman:
    """Minimaler Ersatz für die Spoolman-REST-API."""

    def __init__(self):
        self.spools = {
            1: {"id": 1, "location": "Lager", "extra": {"nfc_id": json.dumps("04A1B2C3")},
                "filament": {"material": "PLA", "color_hex": "FF0000", "name": "Rot",
                             "vendor": {"name": "Polytera"}}},
            2: {"id": 2, "location": "X2D AMS A3", "extra": {},
                "filament": {"material": "PETG", "color_hex": "000000", "name": "Schwarz"}},
        }
        self.fields = {"spool": [], "filament": []}

    def app(self):
        a = web.Application()

        async def get_fields(r):
            return web.json_response(self.fields[r.match_info["e"]])

        async def add_field(r):
            body = await r.json()
            self.fields[r.match_info["e"]].append({"key": r.match_info["k"], **body})
            return web.json_response({})

        async def list_spools(r):
            return web.json_response(list(self.spools.values()))

        async def get_spool(r):
            return web.json_response(self.spools[int(r.match_info["id"])])

        async def patch_spool(r):
            s = self.spools[int(r.match_info["id"])]
            s.update(await r.json())
            return web.json_response(s)

        a.router.add_get("/api/v1/field/{e}", get_fields)
        a.router.add_post("/api/v1/field/{e}/{k}", add_field)
        a.router.add_get("/api/v1/spool", list_spools)
        a.router.add_get("/api/v1/spool/{id}", get_spool)
        a.router.add_patch("/api/v1/spool/{id}", patch_spool)
        return a


class FakePrinter(BambuPrinter):
    def __init__(self, name, on_update, loop):
        super().__init__(name, "127.0.0.1", "SERIAL", "code", on_update, loop)
        self.sent = []
        self.connected = True

    def start(self):
        pass

    def _publish(self, payload):
        self.sent.append(payload)
        cmd = payload.get("print", {})
        if cmd.get("command") == "ams_filament_setting":
            # Drucker bestätigt die Einstellung im nächsten Report
            self.report({"ams": [{"id": str(cmd["ams_id"]), "tray": [
                {"id": str(cmd["tray_id"]), "tray_type": cmd["tray_type"],
                 "tray_info_idx": cmd["tray_info_idx"]}]}]})

    def report(self, ams):
        if self.state.merge(ams):
            self.on_update(self)


@pytest.fixture
async def env(aiohttp_server):
    fake = FakeSpoolman()
    server = await aiohttp_server(fake.app())
    cfg = Config(
        spoolman_url=str(server.make_url("")),
        printers={"x2d": PrinterCfg(name="x2d", type="bambu", location="X2D")},
        readers={"x2d": "x2d"},
        window_seconds=60,
    )
    clock = {"t": 0.0}
    async with ClientSession() as session:
        sm = Spoolman(cfg.spoolman_url, session)
        await sm.ensure_fields()
        bridge = Bridge(cfg, sm, clock=lambda: clock["t"])
        printer = FakePrinter("x2d", bridge.on_bambu_update, asyncio.get_running_loop())
        bridge.add_bambu(printer)
        yield fake, bridge, printer, clock


async def _settle(bridge):
    for _ in range(50):
        if not bridge._tasks:
            return
        await asyncio.sleep(0.05)


async def test_end_to_end_scan_insert(env):
    fake, bridge, printer, clock = env
    assert {f["key"] for f in fake.fields["spool"]} == {"nfc_id"}

    empty = [{"id": str(i)} for i in range(4)]
    printer.report({"ams": [{"id": "0", "tray": empty}], "tray_exist_bits": "0"})

    res = await bridge.handle_scan("x2d", "04:a1:b2:c3")
    assert res["result"] == "armed"

    clock["t"] = 5
    printer.report({"tray_exist_bits": "4"})  # Slot A3 (Index 2) belegt
    await _settle(bridge)

    cmd = printer.sent[-1]["print"]
    assert (cmd["ams_id"], cmd["tray_id"], cmd["tray_info_idx"]) == (0, 2, "GFL99")
    assert fake.spools[1]["location"] == "X2D AMS A3"
    assert fake.spools[2]["location"] == "Lager"  # vorherige Spule in dem Slot ausgebucht
    assert bridge.events[0]["level"] == "ok"

    # Spule entnehmen -> zurück ins Lager
    clock["t"] = 10
    printer.report({"tray_exist_bits": "0"})
    await _settle(bridge)
    assert fake.spools[1]["location"] == "Lager"


async def test_unknown_tag_and_link(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    res = await bridge.handle_scan("x2d", "DEADBEEF")
    assert res["result"] == "unknown" and bridge.unknown["uid"] == "DEADBEEF"

    res = await bridge.link_tag("DEADBEEF", 2)
    assert res["result"] == "armed"
    assert json.loads(fake.spools[2]["extra"]["nfc_id"]) == "DEADBEEF"
    assert bridge.unknown is None
