from src.persistence import Persistence


def test_load_energy_register_default(tmp_path):
    p = Persistence(data_dir=str(tmp_path))
    assert p.load_energy_register_wh() == 0


def test_save_and_load_energy_register(tmp_path):
    p = Persistence(data_dir=str(tmp_path))
    p.save_energy_register_wh(12345)
    assert p.load_energy_register_wh() == 12345


def test_load_corrupt_file_returns_default(tmp_path):
    p = Persistence(data_dir=str(tmp_path))
    path = tmp_path / "energy_register.json"
    path.write_text("not json{{{")
    assert p.load_energy_register_wh() == 0


def test_serial_number_generated_and_persisted(tmp_path):
    p = Persistence(data_dir=str(tmp_path))
    serial = p.load_serial_number()
    assert len(serial) == 6
    assert serial.isdigit()
    # Second call returns same value
    assert p.load_serial_number() == serial


import pytest


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        '{"energy_wh":"bad"}',
        '{"energy_wh":-1}',
        '{"energy_wh":1.9}',
        '{"energy_wh":true}',
    ],
)
def test_invalid_energy_register_returns_default(tmp_path, content):
    (tmp_path / "energy_register.json").write_text(content)
    assert Persistence(str(tmp_path)).load_energy_register_wh() == 0


def test_failed_atomic_save_preserves_previous_register(tmp_path, monkeypatch):
    p = Persistence(str(tmp_path))
    p.save_energy_register_wh(12345)

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr("os.replace", fail)
    p.save_energy_register_wh(99999)
    assert p.load_energy_register_wh() == 12345
