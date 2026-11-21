from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Callable, Awaitable

from aiohttp import web

from src.charger_sim import VALID_CURRENT_SETTINGS
from src.shared_state import SharedState

logger = logging.getLogger(__name__)
STATE = web.AppKey("shared_state", SharedState)
PLUG = web.AppKey("on_plug")
UNPLUG = web.AppKey("on_unplug")
CURRENT = web.AppKey("on_set_current")
POWER = web.AppKey("on_set_power")
STATIC = web.AppKey("static_dir", Path)


def create_api_app(
    shared_state: SharedState,
    on_plug: Callable[[], Awaitable[None]] | None = None,
    on_unplug: Callable[[], Awaitable[None]] | None = None,
    on_set_current: Callable[[int], Awaitable[None]] | None = None,
    on_set_power: Callable[[float | None], Awaitable[None]] | None = None,
    traffic_log=None,
) -> web.Application:
    @web.middleware
    async def readonly(request, handler):
        if shared_state.mode == "relay" and request.method not in ("GET", "HEAD"):
            return web.json_response({"message": "Relay mode is read-only"}, status=405)
        return await handler(request)

    app = web.Application(middlewares=[readonly])
    app[STATE] = shared_state
    app[PLUG] = on_plug
    app[UNPLUG] = on_unplug
    app[CURRENT] = on_set_current
    app[POWER] = on_set_power

    # Ingress serves the status page — use relative path for API calls
    static_dir = Path(__file__).parent / "static"
    app.router.add_get("/", handle_index)
    app[STATIC] = static_dir
    app.router.add_get("/api/state", handle_get_state)

    async def traffic(request):
        return web.json_response(
            list(traffic_log.recent) if traffic_log is not None else []
        )

    app.router.add_get("/api/traffic", traffic)
    app.router.add_post("/api/plug", handle_plug)
    app.router.add_post("/api/unplug", handle_unplug)
    app.router.add_post("/api/current", handle_current)
    app.router.add_post("/api/power", handle_power)

    return app


async def handle_index(request: web.Request) -> web.Response:
    static_dir: Path = request.app[STATIC]
    index = static_dir / "index.html"
    return web.FileResponse(index)


async def handle_get_state(request: web.Request) -> web.Response:
    state: SharedState = request.app[STATE]
    return web.json_response(state.to_dict())


async def handle_plug(request: web.Request) -> web.Response:
    await request.app[PLUG]()
    return web.json_response({"status": "ok"})


async def handle_unplug(request: web.Request) -> web.Response:
    await request.app[UNPLUG]()
    return web.json_response({"status": "ok"})


async def handle_current(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        amps = body["amps"]
        if type(amps) is not int:
            raise ValueError("amps must be an integer")
    except (ValueError, KeyError, TypeError):
        return web.json_response(
            {"status": "error", "message": 'Invalid request. Send JSON: {"amps": N}'},
            status=400,
        )
    if amps not in VALID_CURRENT_SETTINGS:
        return web.json_response(
            {
                "status": "error",
                "message": f"Invalid current. Valid: {VALID_CURRENT_SETTINGS}",
            },
            status=400,
        )
    await request.app[CURRENT](amps)
    return web.json_response({"status": "ok"})


async def handle_power(request: web.Request) -> web.Response:
    on_set_power = request.app[POWER]
    if on_set_power is None:
        return web.json_response(
            {"status": "error", "message": "Power override not supported"},
            status=501,
        )
    try:
        body = await request.json()
        power_kw = body["power_kw"]
        if power_kw is not None:
            if type(power_kw) not in (int, float) or not math.isfinite(power_kw):
                raise ValueError("power_kw must be a finite number or null")
            power_kw = float(power_kw)
    except (ValueError, KeyError, TypeError):
        return web.json_response(
            {
                "status": "error",
                "message": 'Invalid request. Send JSON: {"power_kw": N} or {"power_kw": null}',
            },
            status=400,
        )
    await on_set_power(power_kw)
    return web.json_response({"status": "ok"})
