from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class Config:
    server_hostname: str
    chargepoint_id: str
    password: str
    charger_model: str
    charger_vendor: str
    charger_serial: str
    firmware_version: str
    current_amps: int
    initial_energy_wh: int
    log_level: str
    use_tls: bool = True

    mode: str = "emulator"
    relay_host: str = "0.0.0.0"
    relay_port: int = 9000
    relay_certfile: str = ""
    relay_keyfile: str = ""

    def __post_init__(self):
        if (
            not self.server_hostname
            or any(char in self.server_hostname for char in "/@?#")
            or any(char.isspace() for char in self.server_hostname)
        ):
            raise ValueError("server_hostname must be a hostname with an optional port")
        endpoint = urlsplit("wss://" + self.server_hostname)
        if not endpoint.hostname or endpoint.port == 0:
            raise ValueError("Invalid server hostname or port")
        if not self.chargepoint_id or ":" in self.chargepoint_id or not self.password:
            raise ValueError("A valid chargepoint ID and password are required")
        if self.current_amps not in (6, 10, 13, 16, 20, 25, 32):
            raise ValueError("current_amps must be one of 6/10/13/16/20/25/32")
        if self.initial_energy_wh < 0:
            raise ValueError("initial_energy_wh must be non-negative")
        if self.mode not in ("emulator", "relay"):
            raise ValueError("mode must be emulator or relay")
        if bool(self.relay_certfile) != bool(self.relay_keyfile):
            raise ValueError("Both relay TLS certificate and key are required")
        if self.mode == "relay" and (not self.chargepoint_id or not self.password):
            raise ValueError("Relay requires a chargepoint ID and password")
        if not 0 <= self.relay_port <= 65535:
            raise ValueError("Invalid relay port")

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.use_tls else "ws"
        return f"{scheme}://{quote(self.chargepoint_id, safe='')}:{quote(self.password, safe='')}@{self.server_hostname}/{quote(self.chargepoint_id, safe='')}"

    @property
    def redacted_url(self) -> str:
        scheme = "wss" if self.use_tls else "ws"
        return f"{scheme}://{self.chargepoint_id}:***@{self.server_hostname}/{self.chargepoint_id}"


def load_config() -> Config:
    return Config(
        server_hostname=os.environ["IO_SERVER_HOSTNAME"],
        chargepoint_id=os.environ["IO_CHARGEPOINT_ID"],
        password=os.environ["IO_PASSWORD"],
        charger_model=os.environ.get("IO_CHARGER_MODEL", "PLP2-0-2-2"),
        charger_vendor=os.environ.get("IO_CHARGER_VENDOR", "Wall Box Chargers"),
        charger_serial=os.environ.get("IO_CHARGER_SERIAL", ""),
        firmware_version=os.environ.get("IO_FIRMWARE_VERSION", "6.11.16"),
        current_amps=int(os.environ.get("IO_CURRENT_AMPS", "32")),
        initial_energy_wh=int(os.environ.get("IO_INITIAL_ENERGY_WH", "0")),
        log_level=os.environ.get("IO_LOG_LEVEL", "info"),
        mode=os.environ.get("IO_MODE", "emulator"),
        relay_port=int(os.environ.get("IO_RELAY_PORT", "9000")),
        relay_certfile=os.environ.get("IO_RELAY_CERTFILE", ""),
        relay_keyfile=os.environ.get("IO_RELAY_KEYFILE", ""),
        use_tls=os.environ.get("IO_USE_TLS", "true").lower() != "false",
    )
