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
        self.vendors = []
        self.filaments = []

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

        async def list_vendors(r):
            return web.json_response(self.vendors)

        async def add_vendor(r):
            v = {"id": len(self.vendors) + 1, **(await r.json())}
            self.vendors.append(v)
            return web.json_response(v)

        async def list_filaments(r):
            return web.json_response(self.filaments)

        async def add_filament(r):
            body = await r.json()
            vendor = next(v for v in self.vendors if v["id"] == body.pop("vendor_id"))
            f = {"id": len(self.filaments) + 1, "vendor": vendor, **body}
            self.filaments.append(f)
            return web.json_response(f)

        async def add_spool(r):
            body = await r.json()
            fid = body.pop("filament_id")
            fil = next(f for f in self.filaments if f["id"] == fid)
            sid = max(self.spools) + 1
            self.spools[sid] = {"id": sid, "filament": fil, **body}
            return web.json_response(self.spools[sid])

        async def patch_filament(r):
            f = next(f for f in self.filaments if f["id"] == int(r.match_info["id"]))
            f.update(await r.json())
            return web.json_response(f)

        a.router.add_patch("/api/v1/filament/{id}", patch_filament)
        a.router.add_get("/api/v1/vendor", list_vendors)
        a.router.add_post("/api/v1/vendor", add_vendor)
        a.router.add_get("/api/v1/filament", list_filaments)
        a.router.add_post("/api/v1/filament", add_filament)
        a.router.add_post("/api/v1/spool", add_spool)
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
    assert {f["key"] for f in fake.fields["spool"]} == {"nfc_id", "bambu_uuid", "home_location"}

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


BAMBU_TRAY = {"id": "1", "tray_type": "PLA", "tray_sub_brands": "PLA Basic",
              "tray_color": "0086D6FF", "tray_info_idx": "GFA00", "tray_weight": "1000",
              "tray_diameter": "1.75", "nozzle_temp_min": "190", "nozzle_temp_max": "230",
              "tray_uuid": "A1B2C3D4E5F60718293A4B5C6D7E8F90", "remain": 80}


async def test_bambu_rfid_spool_created_and_synced(env):
    fake, bridge, printer, clock = env
    trays = [{"id": str(i)} for i in range(4)]
    printer.report({"ams": [{"id": "0", "tray": trays}], "tray_exist_bits": "0"})

    printer.report({"ams": [{"id": "0", "tray": [BAMBU_TRAY]}], "tray_exist_bits": "2"})
    await _settle(bridge)
    new = [s for s in fake.spools.values() if s["id"] > 2]
    assert len(new) == 1
    sp = new[0]
    assert sp["location"] == "X2D AMS A2" and sp["remaining_weight"] == 800
    assert sp["filament"]["vendor"]["name"] == "Bambu Lab"
    assert sp["filament"]["color_hex"] == "0086D6"

    # Restmenge sinkt -> Spoolman wird nachgeführt
    printer.report({"ams": [{"id": "0", "tray": [{**BAMBU_TRAY, "remain": 65}]}]})
    await _settle(bridge)
    assert fake.spools[sp["id"]]["remaining_weight"] == 650

    # rausnehmen und wieder einstecken -> keine zweite Spule, kein zweites Filament
    printer.report({"ams": [{"id": "0", "tray": [{"id": "1"}]}], "tray_exist_bits": "0"})
    await _settle(bridge)
    printer.report({"ams": [{"id": "0", "tray": [{**BAMBU_TRAY, "id": "3", "remain": 65}]}],
                    "tray_exist_bits": "8"})
    await _settle(bridge)
    assert len([s for s in fake.spools.values() if s["id"] > 2]) == 1
    assert len(fake.filaments) == 1
    assert fake.spools[sp["id"]]["location"] == "X2D AMS A4"


async def test_scan_never_lands_on_bambu_slot(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    printer.report({"ams": [{"id": "0", "tray": [BAMBU_TRAY]}], "tray_exist_bits": "2"})
    await _settle(bridge)
    res = await bridge.handle_scan("x2d", "04A1B2C3")
    assert res["result"] == "armed"  # nicht dem Bambu-Slot zugeordnet


async def test_scale_sets_remaining_weight(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    # Leergewicht beim Filament hinterlegt
    fake.spools[1]["filament"]["spool_weight"] = 180
    res = await bridge.handle_scan("x2d", "04A1B2C3", weight=845.4)
    assert res["result"] == "armed"
    assert fake.spools[1]["remaining_weight"] == 665.4

    # Leergewicht direkt an der Spule hat Vorrang
    fake.spools[1]["spool_weight"] = 200
    await bridge.handle_scan("x2d", "04A1B2C3", weight=845.4)
    assert fake.spools[1]["remaining_weight"] == 645.4


async def test_scale_without_empty_weight_warns(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    await bridge.handle_scan("x2d", "04A1B2C3", weight=900)
    assert "remaining_weight" not in fake.spools[1]
    assert any("Leergewicht" in e["text"] for e in bridge.events)


async def test_scale_only_reader(env):
    fake, bridge, printer, clock = env
    bridge.cfg.readers["waage"] = None
    fake.spools[1]["spool_weight"] = 200
    res = await bridge.handle_scan("waage", "04A1B2C3", weight=700)
    assert res["result"] == "weighed"
    assert fake.spools[1]["remaining_weight"] == 500
    assert bridge.detectors["x2d"].armed_spool is None


async def test_web_scan_weight_parsing(env, aiohttp_client):
    from bridge.web import make_app
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    fake.spools[1]["spool_weight"] = 200
    client = await aiohttp_client(make_app(bridge))
    r = await client.post("/api/scan", json={"reader": "x2d", "uid": "04-A1-B2-C3", "weight": ""})
    assert r.status == 200 and "remaining_weight" not in fake.spools[1]
    r = await client.post("/api/scan", json={"reader": "x2d", "uid": "04-A1-B2-C3", "weight": "1200.0"})
    assert r.status == 200 and fake.spools[1]["remaining_weight"] == 1000


def test_uid_matches_padding():
    from bridge.spoolman import uid_matches, spool_tags
    assert uid_matches("4D3E2A1B00000000", "4D-3E-2A-1B")
    assert not uid_matches("4D3E2A1B00000001", "4D3E2A1B")
    assert not uid_matches("", "4D3E2A1B")
    assert spool_tags({"extra": {"nfc_id": json.dumps("aa11, BB22")}}) == ["AA11", "BB22"]


async def test_bambu_tag_uid_learned_from_ams(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    tray = {**BAMBU_TRAY, "tag_uid": "4D3E2A1B00000000"}
    printer.report({"ams": [{"id": "0", "tray": [tray]}], "tray_exist_bits": "2"})
    await _settle(bridge)
    sp = next(s for s in fake.spools.values() if s["id"] > 2)
    assert json.loads(sp["extra"]["nfc_id"]) == "4D3E2A1B00000000"

    # Leser liest dieselbe Seite -> Spule erkannt, kein "unbekannter Tag"
    found = await bridge.spoolman.find_by_uid("4D-3E-2A-1B")
    assert found["id"] == sp["id"]

    # zweite Seite der Spule manuell verknüpfen -> beide Tags hängen an der Spule
    await bridge.spoolman.link_uid(sp["id"], "77665544")
    assert json.loads(fake.spools[sp["id"]]["extra"]["nfc_id"]) == "4D3E2A1B00000000,77665544"


async def test_link_moves_tag_between_spools(env):
    fake, bridge, printer, clock = env
    await bridge.spoolman.link_uid(2, "04A1B2C3")  # war an Spule 1
    assert json.loads(fake.spools[1]["extra"]["nfc_id"]) == ""
    assert json.loads(fake.spools[2]["extra"]["nfc_id"]) == "04A1B2C3"


async def test_scan_response_has_display_fields(env):
    fake, bridge, printer, clock = env
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    fake.spools[1]["spool_weight"] = 200
    res = await bridge.handle_scan("x2d", "04A1B2C3", weight=845)
    assert (res["vendor"], res["material"], res["name"], res["remaining"]) == \
        ("Polytera", "PLA", "Rot", "645")


def test_product_line():
    from bridge.filament import product_line
    assert product_line("PLA Matte", "PLA") == "Matte"
    assert product_line("PLA Basic", "PLA") == "Basic"
    assert product_line("Support for PLA", "PLA-S") == "Support for PLA"
    assert product_line("PLA", "PLA") == "PLA"


def test_labels_without_id_and_vendor():
    from bridge.spoolman import spool_choice_label, spool_label
    sp = {"id": 16, "remaining_weight": 645.2, "location": "Lager",
          "filament": {"material": "PLA", "name": "Matte Dark Brown", "vendor": {"name": "Bambu Lab"}}}
    assert spool_label(sp) == "PLA Matte Dark Brown"
    assert spool_choice_label(sp) == "PLA Matte Dark Brown (Bambu Lab, 645 g, Lager)"
    sp["filament"]["name"] = "PLA Matte"
    assert spool_label(sp) == "PLA Matte"


async def test_spool_returns_to_home_location(env):
    fake, bridge, printer, clock = env
    fake.spools[1]["location"] = "PLA"
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    await bridge.handle_scan("x2d", "04A1B2C3")
    clock["t"] = 3
    printer.report({"tray_exist_bits": "1"})
    await _settle(bridge)
    assert fake.spools[1]["location"] == "X2D AMS A1"
    assert json.loads(fake.spools[1]["extra"]["home_location"]) == "PLA"

    # umstecken in anderen Slot ändert den Stammplatz nicht
    clock["t"] = 10
    printer.report({"tray_exist_bits": "0"})
    await _settle(bridge)
    assert fake.spools[1]["location"] == "PLA"  # zurück an den Stammplatz, nicht "Lager"


async def test_bambu_spool_uses_spoolmandb_filament(env):
    fake, bridge, printer, clock = env
    # wie aus der SpoolmanDB importiert: keine Bambu-ID, leicht anderer Farbcode
    fake.vendors.append({"id": 1, "name": "Bambu Lab", "empty_spool_weight": 250})
    fake.filaments += [
        {"id": 1, "name": "Basic Blue", "material": "PLA", "color_hex": "0A2CA5",
         "vendor": fake.vendors[0], "extra": {}},
        {"id": 2, "name": "Matte Marine Blue", "material": "PLA", "color_hex": "0B8BD9",
         "vendor": fake.vendors[0], "extra": {}},
        {"id": 3, "name": "Matte Marine Blue", "material": "PETG", "color_hex": "0086D6",
         "vendor": fake.vendors[0], "extra": {}},
    ]
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    tray = {**BAMBU_TRAY, "tray_sub_brands": "PLA Matte", "tray_info_idx": "GFA01"}
    printer.report({"ams": [{"id": "0", "tray": [tray]}], "tray_exist_bits": "2"})
    await _settle(bridge)
    assert len(fake.filaments) == 3  # kein Duplikat angelegt
    sp = next(s for s in fake.spools.values() if s["id"] > 2)
    assert sp["filament"]["id"] == 2
    assert json.loads(fake.filaments[1]["extra"]["bambu_idx"]) == "GFA01"


async def test_link_creates_spool_from_filament(env):
    fake, bridge, printer, clock = env
    fake.vendors.append({"id": 1, "name": "SUNLU"})
    fake.filaments.append({"id": 1, "name": "Matte Black", "material": "PLA",
                           "color_hex": "000000", "vendor": fake.vendors[0], "spool_weight": 135})
    printer.report({"ams": [{"id": "0", "tray": [{"id": str(i)} for i in range(4)]}],
                    "tray_exist_bits": "0"})
    await bridge.handle_scan("x2d", "AABBCCDD", weight=1100)
    st = await bridge.state()
    assert st["filaments"][0]["label"] == "PLA Matte Black (SUNLU)"
    res = await bridge.link_tag("AABBCCDD", filament_id=1)
    assert res["result"] == "armed"
    sp = next(s for s in fake.spools.values() if s["id"] > 2)
    assert json.loads(sp["extra"]["nfc_id"]) == "AABBCCDD"
    assert sp["remaining_weight"] == 965  # Gewicht vom Scan übernommen
