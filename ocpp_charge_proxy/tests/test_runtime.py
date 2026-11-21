"""Run the actual app entry point with local OCPP/API sockets."""

import asyncio
from unittest.mock import MagicMock

from aiohttp import ClientSession
import pytest
import websockets
from ocpp.v16 import call
from src import __main__ as main
from tests.test_integration import MockServer


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("IO_SERVER_HOSTNAME", "localhost")
    monkeypatch.setenv("IO_CHARGEPOINT_ID", "CP001")
    monkeypatch.setenv("IO_PASSWORD", "local-test")
    monkeypatch.setenv("IO_CHARGER_SERIAL", "test-serial")
    monkeypatch.setenv("IO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IO_USE_TLS", "false")
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    sites = []
    original = main.web.TCPSite

    def site(runner, host, port):
        result = original(runner, "127.0.0.1", 0)
        sites.append(result)
        return result

    monkeypatch.setattr(main.web, "TCPSite", site)
    return sites


async def api_url(sites):
    async with asyncio.timeout(2):
        while not sites or not sites[0]._server:
            await asyncio.sleep(0.001)
    return "http://127.0.0.1:" + str(sites[0]._server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_full_emulator_runtime_and_api(runtime, monkeypatch):
    instances = []
    ready = asyncio.Event()

    async def provider(ws):
        server = MockServer("CP001", ws)
        instances.append(server)
        ready.set()
        try:
            await server.start()
        except websockets.ConnectionClosed:
            pass

    async with websockets.serve(
        provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]
    ) as upstream:
        monkeypatch.setenv(
            "IO_SERVER_HOSTNAME", f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}"
        )
        task = asyncio.create_task(main.run())
        try:
            url = await api_url(runtime)
            await asyncio.wait_for(ready.wait(), 2)
            server = instances[0]
            await asyncio.wait_for(server.boot_received.wait(), 2)
            async with ClientSession() as api:
                async with asyncio.timeout(2):
                    while not (await (await api.get(url + "/api/state")).json())[
                        "connected_to_server"
                    ]:
                        await asyncio.sleep(0.001)
                assert (
                    await api.post(url + "/api/current", json={"amps": 16})
                ).status == 200
                assert (
                    await api.post(url + "/api/power", json={"power_kw": 2.0})
                ).status == 200
                assert (await api.post(url + "/api/plug")).status == 200
                response = await asyncio.wait_for(
                    server.call(call.RemoteStartTransactionPayload(id_tag="RUNTIME")), 2
                )
                assert response.status == "Accepted"
                await asyncio.wait_for(server.start_tx_received.wait(), 2)
                # Wait for transaction startup to finish before unplugging.
                async with asyncio.timeout(2):
                    while (await (await api.get(url + "/api/state")).json())[
                        "state"
                    ] != "Charging":
                        await asyncio.sleep(0.001)
                assert (await api.post(url + "/api/unplug")).status == 200
                assert server.last_stop_tx["transaction_id"] == 42
                assert (await (await api.get(url + "/api/state")).json())[
                    "state"
                ] == "Available"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_relay_entrypoint_never_constructs_emulator(
    runtime, monkeypatch, tmp_path
):
    import base64
    from src.relay import RelayServer

    monkeypatch.setenv("IO_MODE", "relay")
    monkeypatch.setenv("IO_RELAY_PORT", "0")
    monkeypatch.setattr(
        main, "ChargePoint", MagicMock(side_effect=AssertionError("no emulator"))
    )
    monkeypatch.setattr(
        main,
        "Persistence",
        MagicMock(side_effect=AssertionError("no emulator persistence")),
    )
    relays = []
    original = RelayServer.__aenter__

    async def entered(self):
        result = await original(self)
        relays.append(self)
        return result

    monkeypatch.setattr(RelayServer, "__aenter__", entered)

    async def provider(ws):
        async for frame in ws:
            await ws.send(frame)

    async with websockets.serve(
        provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]
    ) as upstream:
        monkeypatch.setenv(
            "IO_SERVER_HOSTNAME", f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}"
        )
        task = asyncio.create_task(main.run())
        try:
            url = await api_url(runtime)
            relay = relays[0]
            token = base64.b64encode(b"CP001:local-test").decode()
            endpoint = relay.url.replace("0.0.0.0", "127.0.0.1")
            async with websockets.connect(
                endpoint,
                subprotocols=["ocpp1.6"],
                extra_headers={"Authorization": "Basic " + token},
            ) as car:
                raw = '[2,"one","Heartbeat",{}]'
                await car.send(raw)
                assert await asyncio.wait_for(car.recv(), 2) == raw
                async with ClientSession() as api:
                    assert (await api.post(url + "/api/plug")).status == 405
                    data = await (await api.get(url + "/api/state")).json()
                    assert data["mode"] == "relay" and data["frames_forwarded"] == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (tmp_path / "relay" / "traffic.jsonl").exists()
