"""Authenticated, single-charger OCPP transport relay; no OCPP commands originate here."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import http
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import ssl
from urllib.parse import quote
import uuid

import websockets
from websockets.exceptions import ConnectionClosed


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    if isinstance(value, dict):
        # OCPP represents configuration secrets as {key: AuthorizationKey, value: ...}.
        if any(
            word in str(value.get("key", "")).lower()
            for word in ("authorization", "password", "token", "secret", "key")
        ):
            value = {**value, "value": "[REDACTED]"} if "value" in value else value
        return {
            key: (
                "[REDACTED]"
                if any(
                    word in key.lower()
                    for word in (
                        "password",
                        "authorization",
                        "token",
                        "idtag",
                        "secret",
                        "location",
                    )
                )
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class StrictFileHandler(RotatingFileHandler):
    def handleError(self, record):
        raise OSError("Traffic log write failed")

    def _open(self):
        stream = super()._open()
        Path(self.baseFilename).chmod(0o600)
        return stream


class TrafficLog:
    """Bounded asynchronous audit log. Lost records are visible in API state."""

    def __init__(self, directory, state, *, max_bytes=5 * 1024 * 1024, backups=5):
        self.directory = Path(directory)
        self.state = state
        self.max_bytes = max_bytes
        self.backups = backups
        self.recent = deque(maxlen=100)
        self.queue = asyncio.Queue(maxsize=256)

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handler = StrictFileHandler(
            self.directory / "traffic.jsonl",
            maxBytes=self.max_bytes,
            backupCount=self.backups,
            encoding="utf-8",
        )
        self.worker = asyncio.create_task(self._write())
        return self

    async def __aexit__(self, *args):
        await self.queue.put(None)
        await self.worker
        self.handler.close()

    async def _write(self):
        while (row := await self.queue.get()) is not None:
            try:
                record = logging.LogRecord(
                    "traffic", logging.INFO, "", 0, json.dumps(row), (), None
                )
                await asyncio.to_thread(self.handler.emit, record)
            except OSError:
                self.state.log_error = "Traffic log write failed; check storage"
                self.state.log_dropped += 1

    def record(self, session, event, **fields):
        row = dict(timestamp=utc_now(), session_id=session, event=event, **fields)
        self.recent.append(row)
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.state.log_dropped += 1

    def frame(self, session, direction, raw):
        data = raw.encode() if isinstance(raw, str) else raw
        fields = dict(
            direction=direction,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
        )
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
                if isinstance(message, list) and len(message) >= 3:
                    fields.update(
                        message=redact(message),
                        message_type=message[0],
                        message_id=message[1],
                    )
                    if message[0] == 2:
                        fields["action"] = message[2]
            except (ValueError, TypeError, RecursionError):
                pass
        self.record(session, "frame", **fields)


class RelayServer:
    def __init__(self, config, state, audit):
        self.config, self.state, self.audit = config, state, audit
        self.active = False
        state.mode = "relay"
        state.state = "Unavailable"
        state.power_source = "wallbox"
        for key in (
            "plugged_in",
            "power_kw",
            "voltage",
            "current_a",
            "frequency_hz",
            "power_offered_kw",
            "energy_kwh",
            "current_amps_setting",
        ):
            setattr(state, key, None)

    async def __aenter__(self):
        tls = None
        if self.config.relay_certfile:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.config.relay_certfile, self.config.relay_keyfile)
        # websockets DEBUG includes handshake credentials and complete messages.
        transport_logger = logging.getLogger("ocpp_relay_transport")
        transport_logger.setLevel(logging.CRITICAL)
        self.transport_logger = transport_logger
        self.server = await websockets.serve(
            self.handle,
            self.config.relay_host,
            self.config.relay_port,
            subprotocols=["ocpp1.6"],
            process_request=self.authorize,
            ssl=tls,
            logger=transport_logger,
            close_timeout=3,
            max_size=1024 * 1024,
        )
        port = self.server.sockets[0].getsockname()[1]
        scheme = "wss" if tls else "ws"
        self.url = f"{scheme}://{self.config.relay_host}:{port}/{quote(self.config.chargepoint_id, safe='')}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()

    async def authorize(self, path, headers):
        def reject(code):
            return http.HTTPStatus(code), [], b"Connection refused\n"

        if path != "/" + quote(self.config.chargepoint_id, safe=""):
            return reject(404)
        expected = (
            "Basic "
            + base64.b64encode(
                f"{self.config.chargepoint_id}:{self.config.password}".encode()
            ).decode()
        )
        values = headers.get_all("Authorization")
        if len(values) != 1 or not hmac.compare_digest(
            values[0].encode(), expected.encode()
        ):
            return reject(401)
        protocols = ",".join(headers.get_all("Sec-WebSocket-Protocol")).split(",")
        if "ocpp1.6" not in [p.strip() for p in protocols]:
            return reject(400)
        if self.active:
            return reject(409)

    async def handle(self, charger):
        # No await between checking and claiming, including concurrent handshakes.
        if self.active:
            await charger.close(1013, "Charger already connected")
            return
        self.active = True
        session = uuid.uuid4().hex
        self.state.session_id = session
        self.state.charger_connected = True
        self.audit.record(session, "charger_connected")
        tasks = []
        try:
            async with self.provider_connection(charger) as provider:
                if provider is None:
                    return
                if provider.subprotocol != "ocpp1.6":
                    raise ValueError("Provider did not negotiate OCPP 1.6")
                self.state.connected_to_server = True
                self.audit.record(session, "provider_connected")
                tasks = [
                    asyncio.create_task(
                        self.pump(charger, provider, session, "charger_to_provider")
                    ),
                    asyncio.create_task(
                        self.pump(provider, charger, session, "provider_to_charger")
                    ),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(result, Exception) for result in results):
                    self.audit.record(session, "forwarding_failed")
        except (OSError, websockets.WebSocketException, ValueError):
            # Exception text can contain upstream URLs or credentials.
            self.audit.record(session, "connection_failed")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await charger.close(1011, "Relay connection ended; reconnect")
            self.state.connected_to_server = False
            self.state.charger_connected = False
            self.state.state = "Unavailable"
            self.state.plugged_in = None
            self.state.power_kw = self.state.current_a = None
            self.audit.record(session, "disconnected")
            self.active = False

    @asynccontextmanager
    async def provider_connection(self, charger):
        connection = asyncio.ensure_future(
            websockets.connect(
                self.config.websocket_url,
                subprotocols=["ocpp1.6"],
                logger=self.transport_logger,
                open_timeout=15,
                close_timeout=3,
                max_size=1024 * 1024,
            )
        )
        closed = asyncio.create_task(charger.wait_closed())
        provider = None
        try:
            await asyncio.wait(
                [connection, closed], return_when=asyncio.FIRST_COMPLETED
            )
            if closed.done():
                yield None
                return
            provider = await connection
            yield provider
        finally:
            closed.cancel()
            connection.cancel()
            results = await asyncio.gather(connection, closed, return_exceptions=True)
            # The handshake may finish simultaneously with charger closure.
            if provider is None and isinstance(
                results[0], websockets.WebSocketClientProtocol
            ):
                provider = results[0]
            if provider is not None:
                await provider.close()

    async def pump(self, source, destination, session, direction):
        try:
            async for raw in source:
                await destination.send(raw)
                self.state.frames_forwarded += 1
                self.state.last_message_at = utc_now()
                self.audit.frame(session, direction, raw)
                if direction == "charger_to_provider":
                    self.observe(raw)
        except ConnectionClosed as exc:
            self.audit.record(
                session, "peer_closed", direction=direction, close_code=exc.code
            )
            return
        finally:
            self.audit.record(
                session,
                "stream_ended",
                direction=direction,
                close_code=source.close_code,
            )

    def observe(self, raw):
        """Read copies only; malformed/vendor-specific payloads still pass unchanged."""
        try:
            msg = json.loads(raw)
            if msg[0] != 2 or not isinstance(msg[3], dict):
                return
            body = msg[3]
            if body.get("connectorId") != 1:
                return
            if msg[2] == "StatusNotification":
                self.state.state = body["status"]
                self.state.plugged_in = body["status"] in (
                    "Preparing",
                    "Charging",
                    "SuspendedEV",
                    "SuspendedEVSE",
                    "Finishing",
                )
            if msg[2] == "MeterValues":
                for meter in body["meterValue"]:
                    for sample in meter["sampledValue"]:
                        if sample.get("format", "Raw") != "Raw" or sample.get("phase"):
                            continue
                        measurand = sample.get(
                            "measurand", "Energy.Active.Import.Register"
                        )
                        unit = sample.get("unit", "Wh")
                        value = float(sample["value"])
                        if measurand == "Power.Active.Import" and unit in ("W", "kW"):
                            self.state.power_kw = value / 1000 if unit == "W" else value
                        elif measurand == "Energy.Active.Import.Register" and unit in (
                            "Wh",
                            "kWh",
                        ):
                            self.state.energy_kwh = (
                                value / 1000 if unit == "Wh" else value
                            )
                        elif measurand == "Current.Import" and unit == "A":
                            self.state.current_a = value
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            RecursionError,
        ):
            pass
