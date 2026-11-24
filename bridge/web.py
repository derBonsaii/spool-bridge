"""HTTP-API für die Leser und eine kleine Status-Seite."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .service import Bridge

STATIC = Path(__file__).parent / "static"


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application()

    async def index(request):
        return web.FileResponse(STATIC / "index.html")

    async def state(request):
        return web.json_response(await bridge.state())

    async def scan(request):
        data = await request.json()
        reader, uid = str(data.get("reader", "")), str(data.get("uid", ""))
        if not reader or not uid:
            return web.json_response({"result": "error", "message": "reader und uid nötig"}, status=400)
        res = await bridge.handle_scan(reader, uid)
        status = {"unknown": 404, "error": 400}.get(res["result"], 200)
        return web.json_response(res, status=status)

    async def link(request):
        data = await request.json()
        res = await bridge.link_tag(str(data["uid"]), int(data["spool_id"]))
        return web.json_response(res)

    async def assign(request):
        data = await request.json()
        printer = str(data["printer"])
        if printer not in bridge.bambu:
            return web.json_response({"result": "error", "message": "kein Bambu-Drucker"}, status=400)
        slot = (int(data["ams_id"]), int(data["slot"]))
        spool = await bridge.spoolman.spool(int(data["spool_id"]))
        await bridge.assign_bambu(printer, slot, spool)
        return web.json_response({"result": "assigned"})

    async def health(request):
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/scan", scan)
    app.router.add_post("/api/link", link)
    app.router.add_post("/api/assign", assign)
    app.router.add_get("/health", health)
    return app
