"""Companion tests import the real Home Assistant runtime, not stand-in HA classes."""

from types import MappingProxyType
from unittest.mock import patch, MagicMock

from aiohttp import web, ClientSession
import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import HomeAssistantError

from custom_components.ocpp_charge_proxy.coordinator import OCPPChargeProxyCoordinator
from custom_components.ocpp_charge_proxy.config_flow import OCPPChargeProxyConfigFlow


@pytest_asyncio.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    async with ClientSession() as session:
        with patch(
            "custom_components.ocpp_charge_proxy.coordinator.async_get_clientsession",
            return_value=session,
        ):
            yield instance
    await instance.async_stop(force=True)


def entry(options=None):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="ocpp_charge_proxy",
        title="OCPP Charge Proxy",
        data={"api_url": "http://proxy"},
        options=options or {},
        source="user",
        unique_id="ocpp_charge_proxy",
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unit,value,expected",
    [
        ("W", "2400", 2.4),
        ("kW", "2.4", 2.4),
        ("MW", "0.0024", 2.4),
        ("W", "nan", None),
        ("W", "inf", None),
        ("VA", "2400", None),
        (None, "2400", None),
        ("W", "unknown", None),
    ],
)
async def test_power_sensor_units_and_unavailable(
    hass, aiohttp_client, unit, value, expected
):
    received = []

    async def state(request):
        return web.json_response({"mode": "emulator"})

    async def power(request):
        received.append(await request.json())
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/power", power)
    client = await aiohttp_client(app)
    hass.states.async_set("sensor.power", value, {"unit_of_measurement": unit})
    coordinator = OCPPChargeProxyCoordinator(
        hass, entry(), str(client.make_url("")).rstrip("/"), "sensor.power"
    )
    await coordinator._async_update_data()
    assert received == [{"power_kw": expected}]


@pytest.mark.asyncio
async def test_relay_never_pushes_power_or_commands(hass, aiohttp_client):
    async def state(request):
        return web.json_response({"mode": "relay"})

    async def forbidden(request):
        raise AssertionError("relay must never POST")

    app = web.Application()
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/power", forbidden)
    client = await aiohttp_client(app)
    coordinator = OCPPChargeProxyCoordinator(
        hass, entry(), str(client.make_url("")).rstrip("/"), "sensor.power"
    )
    coordinator.data = await coordinator._async_update_data()
    with pytest.raises(HomeAssistantError, match="read-only"):
        await coordinator.send_command("/api/plug")


@pytest.mark.asyncio
async def test_options_flow_uses_framework_config_entry(hass):
    config_entry = entry({"power_entity": "sensor.power"})
    hass.config_entries = MagicMock()
    hass.config_entries.async_get_known_entry.return_value = config_entry
    flow = OCPPChargeProxyConfigFlow.async_get_options_flow(config_entry)
    flow.hass = hass
    flow.handler = config_entry.entry_id
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["data_schema"]({}) == {"power_entity": "sensor.power"}
    result = await flow.async_step_init({"power_entity": "sensor.other"})
    assert result["type"] == "create_entry" and result["data"] == {
        "power_entity": "sensor.other"
    }


@pytest.mark.asyncio
async def test_simulator_controls_unavailable_in_relay(hass):
    from custom_components.ocpp_charge_proxy.switch import OCPPChargeProxyPlugSwitch
    from custom_components.ocpp_charge_proxy.select import OCPPChargeProxyCurrentSelect

    config_entry = entry()
    coordinator = OCPPChargeProxyCoordinator(hass, config_entry, "http://proxy")
    coordinator.data = {
        "mode": "relay",
        "plugged_in": True,
        "current_amps_setting": None,
    }
    switch = OCPPChargeProxyPlugSwitch(coordinator, config_entry)
    select = OCPPChargeProxyCurrentSelect(coordinator, config_entry)
    assert not switch.available and not select.available
    coordinator.data["mode"] = "emulator"
    assert switch.available and select.available


@pytest.mark.asyncio
async def test_entity_identity_and_unknown_meter_values_survive_mode_change(hass):
    from custom_components.ocpp_charge_proxy import (
        sensor,
        binary_sensor,
        switch,
        select,
    )
    from custom_components.ocpp_charge_proxy.const import DOMAIN

    config_entry = entry()
    coordinator = OCPPChargeProxyCoordinator(hass, config_entry, "http://proxy")
    coordinator.data = {
        "mode": "relay",
        "power_kw": None,
        "energy_kwh": None,
        "connected_to_server": False,
    }
    hass.data[DOMAIN] = {config_entry.entry_id: coordinator}
    entities = []
    for platform in (sensor, binary_sensor, switch, select):
        await platform.async_setup_entry(
            hass, config_entry, lambda added: entities.extend(added)
        )
    ids = [entity.unique_id for entity in entities]
    assert len(ids) == len(set(ids)) == 8
    meters = [
        entity
        for entity in entities
        if getattr(entity, "entity_description", None)
        and entity.entity_description.key in ("power_kw", "energy_kwh")
    ]
    assert len(meters) == 2 and all(entity.native_value is None for entity in meters)
    coordinator.data.update(mode="emulator", power_kw=2.4, energy_kwh=10.5)
    assert [entity.unique_id for entity in entities] == ids
    assert [entity.native_value for entity in meters] == [2.4, 10.5]


@pytest.mark.asyncio
async def test_setup_poll_then_unload(hass, aiohttp_client):
    from unittest.mock import AsyncMock
    from custom_components import ocpp_charge_proxy as integration
    from custom_components.ocpp_charge_proxy.const import DOMAIN

    async def state(request):
        return web.json_response({"mode": "emulator", "state": "Preparing"})

    app = web.Application()
    app.router.add_get("/api/state", state)
    client = await aiohttp_client(app)
    config_entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="OCPP Charge Proxy",
        data={"api_url": str(client.make_url("")).rstrip("/")},
        options={},
        source="user",
        unique_id=DOMAIN,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    assert await integration.async_setup_entry(hass, config_entry)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    assert coordinator.data["state"] == "Preparing"
    assert await integration.async_unload_entry(hass, config_entry)
    assert config_entry.entry_id not in hass.data[DOMAIN]
    await coordinator.async_shutdown()
