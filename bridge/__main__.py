"""Start: python -m bridge [pfad/zur/config.yaml]"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aiohttp
from aiohttp import web

from . import __version__
from .bambu import BambuPrinter
from .config import load
from .moonraker import Moonraker
from .service import Bridge
from .spoolman import Spoolman
from .web import make_app


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("bridge")
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CONFIG", "/config/config.yaml")
    cfg = load(path)
    log.info("spool-bridge %s, Konfiguration: %s", __version__, path)

    loop = asyncio.get_running_loop()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        spoolman = Spoolman(cfg.spoolman_url, session)
        bridge = Bridge(cfg, spoolman)

        for attempt in range(30):
            try:
                await spoolman.ensure_fields()
                break
            except Exception as e:  # Spoolman startet evtl. noch
                log.warning("Spoolman noch nicht erreichbar (%s) – neuer Versuch in 10 s", e)
                await asyncio.sleep(10)
        else:
            raise SystemExit("Spoolman nicht erreichbar – spoolman_url prüfen")

        for name, p in cfg.printers.items():
            if p.type == "bambu":
                printer = BambuPrinter(name, p.host, p.serial, p.access_code,
                                       on_update=bridge.on_bambu_update, loop=loop)
                bridge.add_bambu(printer)
                printer.start()
            else:
                bridge.add_moonraker(Moonraker(name, p.url, session))

        runner = web.AppRunner(make_app(bridge))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
        log.info("Web-UI und API auf Port %s", cfg.port)

        try:
            await asyncio.Event().wait()
        finally:
            for p in bridge.bambu.values():
                p.stop()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
