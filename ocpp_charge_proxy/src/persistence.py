from __future__ import annotations

import json
import logging
import os
import random
import tempfile

logger = logging.getLogger(__name__)

_ENERGY_FILE = "energy_register.json"
_SERIAL_FILE = "serial_number.json"


class Persistence:
    def __init__(self, data_dir: str = "/data"):
        self._data_dir = data_dir

    def _read(self, filename: str, key: str, default):
        path = os.path.join(self._data_dir, filename)
        try:
            with open(path) as f:
                data = json.load(f)
                return data.get(key, default) if isinstance(data, dict) else default
        except (OSError, ValueError):
            return default

    def _write(self, filename: str, key: str, value):
        path = os.path.join(self._data_dir, filename)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._data_dir, delete=False
            ) as stream:
                temporary = stream.name
                json.dump({key: value}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            logger.warning("Failed to write %s", path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def load_energy_register_wh(self) -> int:
        value = self._read(_ENERGY_FILE, "energy_wh", 0)
        return value if type(value) is int and value >= 0 else 0

    def save_energy_register_wh(self, value: int) -> None:
        self._write(_ENERGY_FILE, "energy_wh", value)

    def load_serial_number(self) -> str:
        serial = self._read(_SERIAL_FILE, "serial", "")
        if not isinstance(serial, str) or not serial:
            serial = str(random.randint(100000, 999999))
            self.save_serial_number(serial)
            logger.info("Generated charger serial number: %s", serial)
        return serial

    def save_serial_number(self, value: str) -> None:
        self._write(_SERIAL_FILE, "serial", value)
