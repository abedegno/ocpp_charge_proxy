"""Real sockets against a local provider; never connect to Octopus in tests."""

import asyncio
import base64
import json
from dataclasses import replace

import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from src.config import load_config
from src.shared_state import SharedState


def config(monkeypatch, **overrides):
    monkeypatch.setenv("IO_SERVER_HOSTNAME", "localhost")
    monkeypatch.setenv("IO_CHARGEPOINT_ID", "charger-1")
    monkeypatch.setenv("IO_PASSWORD", "test-secret")
    return replace(load_config(), **overrides)


def auth(password="test-secret"):
    value = base64.b64encode(f"charger-1:{password}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def test_mode_defaults_to_emulator(monkeypatch):
    assert config(monkeypatch).mode == "emulator"


@pytest.mark.asyncio
async def test_exact_bidirectional_frames_and_no_generated_messages(
    monkeypatch, tmp_path
):
    from src.relay import RelayServer, TrafficLog

    received = []
    profile = '[2, "p-1", "SetChargingProfile", {"connectorId":1,"csChargingProfiles":{"chargingProfileId":8,"stackLevel":2}}]'
    clear = '[2,"p-2","ClearChargingProfile",{"id":8}]'
    ack = '[3, "p-1", {"status":"Accepted"}]'
    boot = '[2,"boot-1","BootNotification",{"chargePointVendor":"Real Wallbox"}]'

    async def provider(ws):
        assert ws.request_headers["Authorization"] == auth()["Authorization"]
        received.append(await ws.recv())
        await ws.send(profile)
        received.append(await ws.recv())
        await ws.send(clear)
        received.append(await ws.recv())
        await ws.send(b"binary-frame")
        await ws.wait_closed()

    async with websockets.serve(
        provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]
    ) as upstream:
        port = upstream.sockets[0].getsockname()[1]
        cfg = config(
            monkeypatch,
            mode="relay",
            use_tls=False,
            server_hostname=f"127.0.0.1:{port}",
            relay_host="127.0.0.1",
            relay_port=0,
        )
        state = SharedState()
        async with TrafficLog(tmp_path, state) as audit:
            async with RelayServer(cfg, state, audit) as relay:
                async with websockets.connect(
                    relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                ) as wallbox:
                    await wallbox.send(boot)
                    assert await wallbox.recv() == profile
                    await wallbox.send(ack)
                    assert await wallbox.recv() == clear
                    await wallbox.send('[4,"p-2","NotSupported","",{}]')
                    assert await wallbox.recv() == b"binary-frame"
                await asyncio.sleep(0.02)
                assert received == [boot, ack, '[4,"p-2","NotSupported","",{}]']
        rows = [
            json.loads(line)
            for line in (tmp_path / "traffic.jsonl").read_text().splitlines()
        ]
        frames = [row for row in rows if row["event"] == "frame"]
        assert len(frames) == 6
        assert {r["direction"] for r in frames} == {
            "charger_to_provider",
            "provider_to_charger",
        }
        assert all(r["session_id"] and r["timestamp"] and r["sha256"] for r in frames)
        assert "test-secret" not in (tmp_path / "traffic.jsonl").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,password,protocol,expected",
    [
        ("/charger-1", "wrong", "ocpp1.6", 401),
        ("/other-charger", "test-secret", "ocpp1.6", 404),
        ("/charger-1?query=yes", "test-secret", "ocpp1.6", 404),
        ("/charger-1", "test-secret", "other", 400),
    ],
)
async def test_invalid_handshakes_never_reach_provider(
    monkeypatch, tmp_path, path, password, protocol, expected
):
    from src.relay import RelayServer, TrafficLog

    cfg = config(monkeypatch, mode="relay", relay_host="127.0.0.1", relay_port=0)
    state = SharedState()
    async with TrafficLog(tmp_path, state) as audit:
        async with RelayServer(cfg, state, audit) as relay:
            with pytest.raises(InvalidStatusCode) as exc:
                async with websockets.connect(
                    relay.url.rsplit("/", 1)[0] + path,
                    subprotocols=[protocol],
                    extra_headers=auth(password),
                ):
                    pass
            assert exc.value.status_code == expected


@pytest.mark.asyncio
async def test_provider_disconnect_couples_sessions_and_never_replays(
    monkeypatch, tmp_path
):
    from src.relay import RelayServer, TrafficLog

    sessions = []
    release = asyncio.Event()

    async def provider(ws):
        sessions.append([await ws.recv()])
        await release.wait()
        await ws.close()

    async with websockets.serve(
        provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]
    ) as upstream:
        cfg = config(
            monkeypatch,
            mode="relay",
            use_tls=False,
            server_hostname=f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}",
            relay_host="127.0.0.1",
            relay_port=0,
        )
        state = SharedState()
        async with TrafficLog(tmp_path, state) as audit:
            async with RelayServer(cfg, state, audit) as relay:
                async with websockets.connect(
                    relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                ) as car:
                    await car.send('[2,"first","Heartbeat",{}]')
                    while not sessions:
                        await asyncio.sleep(0.001)
                    with pytest.raises(InvalidStatusCode) as exc:
                        await websockets.connect(
                            relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                        )
                    assert exc.value.status_code == 409
                    release.set()
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(car.recv(), 2)
                while relay.active:
                    await asyncio.sleep(0.001)
                async with websockets.connect(
                    relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                ) as car:
                    await car.send('[2,"second","Heartbeat",{}]')
                    await asyncio.wait_for(car.wait_closed(), 2)
        assert sessions == [
            ['[2,"first","Heartbeat",{}]'],
            ['[2,"second","Heartbeat",{}]'],
        ]
        assert not state.connected_to_server and not state.charger_connected
        assert state.power_kw is None


@pytest.mark.asyncio
async def test_provider_unavailable_closes_charger_without_credentials_in_logs(
    monkeypatch, tmp_path
):
    from src.relay import RelayServer, TrafficLog

    # Allocate a port and release it rather than relying on a specific unused port.
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    cfg = config(
        monkeypatch,
        mode="relay",
        use_tls=False,
        server_hostname=f"127.0.0.1:{port}",
        relay_host="127.0.0.1",
        relay_port=0,
    )
    state = SharedState()
    async with TrafficLog(tmp_path, state) as audit:
        async with RelayServer(cfg, state, audit) as relay:
            async with websockets.connect(
                relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
            ) as car:
                await asyncio.wait_for(car.wait_closed(), 2)
    content = (tmp_path / "traffic.jsonl").read_text()
    assert "connection_failed" in content
    assert "test-secret" not in content


@pytest.mark.asyncio
async def test_audit_redacts_configuration_credentials_and_rotates(tmp_path):
    from src.relay import TrafficLog

    state = SharedState()
    async with TrafficLog(tmp_path, state, max_bytes=1000, backups=2) as audit:
        for i in range(25):
            audit.frame(
                "s",
                "provider_to_charger",
                '[2,"key","ChangeConfiguration",{"key":"AuthorizationKey","value":"super-secret"}]',
            )
        audit.frame(
            "s", "charger_to_provider", '[2,"tag","Authorize",{"idTag":"private-tag"}]'
        )
    assert (tmp_path / "traffic.jsonl.1").exists()
    for path in tmp_path.glob("traffic.jsonl*"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert "super-secret" not in path.read_text()
        assert "private-tag" not in path.read_text()
    assert "super-secret" not in json.dumps(list(audit.recent))


@pytest.mark.asyncio
async def test_readonly_relay_api(aiohttp_client, tmp_path):
    from src.api import create_api_app
    from src.relay import TrafficLog

    state = SharedState(mode="relay")
    async with TrafficLog(tmp_path, state) as audit:
        audit.record("session", "connected")
        client = await aiohttp_client(create_api_app(state, traffic_log=audit))
        for endpoint in ("plug", "unplug", "current", "power"):
            response = await client.post(
                "/api/" + endpoint, json={"amps": 32, "power_kw": 99}
            )
            assert response.status == 405
        response = await client.get("/api/state")
        assert (await response.json())["mode"] == "relay"
        response = await client.get("/api/traffic")
        assert (await response.json())[0]["event"] == "connected"


@pytest.mark.asyncio
async def test_log_write_failure_and_queue_overflow_are_visible(tmp_path, monkeypatch):
    from src.relay import TrafficLog

    state = SharedState()
    async with TrafficLog(tmp_path, state) as audit:

        def fail(record):
            raise OSError("disk full")

        monkeypatch.setattr(audit.handler, "emit", fail)
        for i in range(300):
            audit.record("s", "test")
        assert state.log_dropped == 44
        assert len(audit.recent) == 100
    assert state.log_dropped == 300
    assert state.log_error is not None


def test_relay_configuration_and_encoded_credentials(monkeypatch):
    with pytest.raises(ValueError):
        config(monkeypatch, mode="typo")
    with pytest.raises(ValueError):
        config(monkeypatch, mode="relay", password="")
    with pytest.raises(ValueError):
        config(monkeypatch, relay_certfile="cert.pem")
    cfg = config(monkeypatch, password="a:@/?#", chargepoint_id="wall box")
    assert cfg.websocket_url == "wss://wall%20box:a%3A%40%2F%3F%23@localhost/wall%20box"


@pytest.fixture
def tls_certificate(tmp_path):
    import subprocess

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.mark.asyncio
async def test_untrusted_provider_certificate_is_rejected(
    monkeypatch, tmp_path, tls_certificate
):
    import ssl
    from src.relay import RelayServer, TrafficLog

    cert, key = tls_certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    reached = []

    async def provider(ws):
        reached.append(True)

    async with websockets.serve(
        provider, "127.0.0.1", 0, ssl=context, subprotocols=["ocpp1.6"]
    ) as upstream:
        cfg = config(
            monkeypatch,
            mode="relay",
            server_hostname=f"localhost:{upstream.sockets[0].getsockname()[1]}",
            relay_host="127.0.0.1",
            relay_port=0,
        )
        state = SharedState()
        async with TrafficLog(tmp_path, state) as audit:
            async with RelayServer(cfg, state, audit) as relay:
                async with websockets.connect(
                    relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                ) as car:
                    await asyncio.wait_for(car.wait_closed(), 3)
        assert not reached
        assert not state.connected_to_server
        assert "connection_failed" in (tmp_path / "traffic.jsonl").read_text()


def test_observed_telemetry_has_no_simulated_defaults(monkeypatch, tmp_path):
    from src.relay import RelayServer, TrafficLog

    state = SharedState()
    relay = RelayServer(
        config(monkeypatch, mode="relay"), state, TrafficLog(tmp_path, state)
    )
    assert state.power_kw is None and state.voltage is None and state.plugged_in is None
    relay.observe('[2,"s","StatusNotification",{"connectorId":1,"status":"Charging"}]')
    relay.observe(
        '[2,"m","MeterValues",{"connectorId":1,"meterValue":[{"sampledValue":[{"measurand":"Power.Active.Import","unit":"W","value":"7200"},{"value":"123456"}]}]}]'
    )
    assert state.state == "Charging" and state.plugged_in
    assert state.power_kw == 7.2 and state.energy_kwh == 123.456
    relay.observe("{}")
    relay.observe("not-json")
    relay.observe('[2,"m","MeterValues",null]')


@pytest.mark.asyncio
async def test_inspection_never_disrupts_forwarding(monkeypatch, tmp_path):
    from src.relay import RelayServer, TrafficLog

    deeply_nested = "[" * 10000 + "0" + "]" * 10000

    async def provider(ws):
        assert await ws.recv() == deeply_nested
        await ws.send('[3,"still-alive",{}]')
        await ws.wait_closed()

    async with websockets.serve(
        provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]
    ) as upstream:
        cfg = config(
            monkeypatch,
            mode="relay",
            use_tls=False,
            relay_host="127.0.0.1",
            relay_port=0,
            server_hostname=f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}",
        )
        state = SharedState()
        async with TrafficLog(tmp_path, state) as audit:
            async with RelayServer(cfg, state, audit) as relay:
                async with websockets.connect(
                    relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                ) as car:
                    await car.send(deeply_nested)
                    assert (
                        await asyncio.wait_for(car.recv(), 2) == '[3,"still-alive",{}]'
                    )
        frames = [
            json.loads(line)
            for line in (tmp_path / "traffic.jsonl").read_text().splitlines()
            if json.loads(line)["event"] == "frame"
        ]
        assert len(frames) == 2


@pytest.mark.asyncio
async def test_audit_removes_credentials_in_download_urls(tmp_path):
    from src.relay import TrafficLog

    async with TrafficLog(tmp_path, SharedState()) as audit:
        audit.frame(
            "s",
            "provider_to_charger",
            '[2,"f","UpdateFirmware",{"location":"https://user:firmware-pass@example.com/fw?token=download-secret"}]',
        )
    content = (tmp_path / "traffic.jsonl").read_text()
    assert "firmware-pass" not in content and "download-secret" not in content


@pytest.mark.asyncio
async def test_charger_disconnect_cancels_stalled_provider_handshake(
    monkeypatch, tmp_path
):
    from src.relay import RelayServer, TrafficLog

    started = asyncio.Event()
    release = asyncio.Event()

    async def stall(path, headers):
        started.set()
        await release.wait()

    async def provider(ws):
        await ws.wait_closed()

    async with websockets.serve(
        provider,
        "127.0.0.1",
        0,
        subprotocols=["ocpp1.6"],
        process_request=stall,
        close_timeout=0.1,
    ) as upstream:
        cfg = config(
            monkeypatch,
            mode="relay",
            use_tls=False,
            relay_host="127.0.0.1",
            relay_port=0,
            server_hostname=f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}",
        )
        state = SharedState()
        try:
            async with TrafficLog(tmp_path, state) as audit:
                async with RelayServer(cfg, state, audit) as relay:
                    car = await websockets.connect(
                        relay.url, subprotocols=["ocpp1.6"], extra_headers=auth()
                    )
                    await asyncio.wait_for(started.wait(), 1)
                    await car.close()

                    async def freed():
                        while relay.active:
                            await asyncio.sleep(0.001)

                    await asyncio.wait_for(freed(), 1)
                    assert not state.charger_connected
        finally:
            release.set()
