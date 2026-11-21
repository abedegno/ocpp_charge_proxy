"""Regression tests for state, accounting and real OCPP request routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocpp.v16 import call, call_result
from ocpp.v16.enums import ChargePointStatus as Status
from src.client import ChargePoint
from src.charger_sim import ChargerReading
from tests.test_integration import FakeConnection, MockServer


@pytest.fixture
def cp(monkeypatch):
    persistence = MagicMock()
    persistence.load_energy_register_wh.return_value = 0
    charger = ChargePoint("CP001", AsyncMock(), persistence)

    async def respond(request):
        if isinstance(request, call.StartTransactionPayload):
            return call_result.StartTransactionPayload(
                transaction_id=42, id_tag_info={"status": "Accepted"}
            )
        return SimpleNamespace()

    charger.call = AsyncMock(side_effect=respond)
    charger._charger_sim.sample = MagicMock(
        return_value=ChargerReading(7.2, 230, 31.3, 50, 7.36, 32)
    )
    return charger


def profile(limit):
    return {
        "chargingProfileId": 1,
        "stackLevel": 0,
        "chargingProfilePurpose": "TxProfile",
        "chargingProfileKind": "Relative",
        "chargingSchedule": {
            "chargingRateUnit": "W",
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit}],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, 4.0])
async def test_positive_profile_caps_delivered_and_offered_power(cp, override):
    cp._profile_scheduler.set_profile(profile(1000))
    cp._power_override = override
    await cp._do_start_transaction()
    await cp.send_meter_values()
    assert cp._shared_state.power_kw == 1.0
    assert cp._shared_state.power_offered_kw == 1.0
    assert cp._shared_state.current_a == pytest.approx(1000 / 230, abs=0.01)


@pytest.mark.asyncio
async def test_clearing_zero_profile_resumes_active_transaction(cp):
    cp._profile_scheduler.set_profile(profile(0))
    await cp._do_start_transaction()
    await cp.send_meter_values()
    assert cp.state == Status.suspended_evse
    cp._profile_scheduler.clear_profile()
    await cp.send_meter_values()
    assert cp.state == Status.charging
    assert cp._charger_sim.is_charging


@pytest.mark.asyncio
async def test_idle_time_is_excluded_and_final_interval_included(cp, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("src.client.time", SimpleNamespace(monotonic=lambda: clock[0]))
    cp._last_meter_time = 100
    clock[0] = 159
    await cp._do_start_transaction()
    clock[0] = 160
    await cp.send_meter_values()
    assert cp._energy_register_wh == 2
    clock[0] = 165
    await cp._do_stop_transaction()
    stop = next(
        args.args[0]
        for args in cp.call.call_args_list
        if isinstance(args.args[0], call.StopTransactionPayload)
    )
    assert stop.meter_stop == 12
    assert cp._energy_register_wh == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ocpp_request",
    [
        call.ChangeAvailabilityPayload(connector_id=1, type="Inoperative"),
        call.ResetPayload(type="Soft"),
    ],
)
async def test_incoming_handler_returns_before_outbound_status_reply(
    tmp_path, ocpp_request
):
    from src.persistence import Persistence

    connection = FakeConnection()
    charger = ChargePoint("CP001", connection.client_side(), Persistence(str(tmp_path)))
    server = MockServer("CP001", connection.server_side())
    tasks = [asyncio.create_task(charger.start()), asyncio.create_task(server.start())]
    try:
        result = await asyncio.wait_for(server.call(ocpp_request), 0.3)
        assert result.status == "Accepted"
        # Give follow-up work a chance to complete and receive its actual response.
        await asyncio.sleep(0.02)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(charger, "shutdown"):
            await charger.shutdown()


@pytest.mark.asyncio
async def test_session_teardown_cancels_meter_and_heartbeat_tasks(cp):
    from src.__main__ import run_emulator_session

    entered = asyncio.Event()
    cleaned = []

    async def receiver():
        await entered.wait()
        raise OSError("connection lost")

    async def loop():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.append("closed")

    cp.start = receiver
    cp.send_boot_notification = AsyncMock(return_value=30)
    cp.heartbeat_loop = lambda interval: loop()
    cp.meter_values_loop = loop
    with pytest.raises(OSError):
        await run_emulator_session(
            cp,
            SimpleNamespace(
                charger_model="x",
                charger_vendor="x",
                charger_serial="x",
                firmware_version="x",
            ),
            MagicMock(),
        )
    assert len(cleaned) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("MeterValueSampleInterval", "invalid"),
        ("MeterValueSampleInterval", "0"),
        ("chargingALimitConn1", "invalid"),
        ("chargingALimitConn1", "15"),
    ],
)
async def test_invalid_configuration_rejected_without_committing(cp, key, value):
    before = dict(cp._config_store)
    result = await cp.on_change_configuration(key, value)
    assert result.status == "Rejected"
    assert cp._config_store == before
    assert key not in cp._server_config


@pytest.mark.asyncio
async def test_duplicate_start_does_not_create_second_transaction(cp):
    cp.state = Status.preparing
    first = await cp.on_remote_start_transaction("TAG", connector_id=1)
    second = await cp.on_remote_start_transaction("TAG", connector_id=1)
    assert first.status == "Accepted" and second.status == "Rejected"
    await asyncio.gather(*cp._background_tasks)
    starts = [
        args
        for args in cp.call.call_args_list
        if isinstance(args.args[0], call.StartTransactionPayload)
    ]
    assert len(starts) == 1 and starts[0].args[0].id_tag == "TAG"


@pytest.mark.asyncio
async def test_scheduled_inoperative_applies_after_stop(cp):
    await cp._do_start_transaction()
    response = await cp.on_change_availability(1, "Inoperative")
    assert response.status == "Scheduled"
    await cp._do_stop_transaction()
    assert cp.state == Status.unavailable


@pytest.mark.asyncio
async def test_pending_start_ack_after_unplug_is_stopped_without_charging(cp):
    waiting = asyncio.Event()
    release = asyncio.Event()
    original = cp.call.side_effect

    async def delayed(request):
        if isinstance(request, call.StartTransactionPayload):
            waiting.set()
            await release.wait()
        return await original(request)

    cp.call.side_effect = delayed
    cp.state = Status.preparing
    await cp.on_remote_start_transaction("TAG", connector_id=1)
    await waiting.wait()
    cp.invalidate_pending_start()
    cp._shared_state.plugged_in = False
    cp.state = Status.available
    release.set()
    await asyncio.gather(*cp._background_tasks)
    assert cp.state == Status.available and cp._transaction_id is None
    assert not cp._charger_sim.is_charging
    assert any(
        isinstance(args.args[0], call.StopTransactionPayload)
        for args in cp.call.call_args_list
    )


@pytest.mark.asyncio
async def test_reconnect_clears_previous_transaction_state(cp):
    await cp._do_start_transaction()
    cp._shared_state.plugged_in = True
    await cp.shutdown()
    assert (
        cp._shared_state.state == "Unavailable"
        and cp._shared_state.transaction_id is None
    )
    replacement = ChargePoint(
        "CP001", AsyncMock(), cp._persistence, shared_state=cp._shared_state
    )
    assert replacement.state == Status.preparing
    assert replacement._shared_state.state == Status.preparing
    assert replacement._shared_state.transaction_id is None


@pytest.mark.asyncio
async def test_slow_stop_does_not_keep_accounting_power(cp, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("src.client.time", SimpleNamespace(monotonic=lambda: clock[0]))
    await cp._do_start_transaction()
    original = cp.call.side_effect
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def delay(request):
        if (
            isinstance(request, call.StatusNotificationPayload)
            and request.status == "Finishing"
        ):
            blocked.set()
            await release.wait()
        return await original(request)

    cp.call.side_effect = delay
    stopping = asyncio.create_task(cp._do_stop_transaction())
    try:
        await blocked.wait()
        clock[0] = 160
        await cp.send_meter_values()
        assert cp._energy_register_wh == 0
    finally:
        release.set()
        await stopping
    states = [
        args.args[0].status
        for args in cp.call.call_args_list
        if isinstance(args.args[0], call.StatusNotificationPayload)
    ]
    assert states[-1] == "Preparing"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_late_rejected_start_preserves_unplugged_state(cp, timeout):
    blocked = asyncio.Event()
    release = asyncio.Event()
    original = cp.call.side_effect

    async def delay(request):
        if isinstance(request, call.StartTransactionPayload):
            blocked.set()
            await release.wait()
            if timeout:
                raise TimeoutError("late timeout")
            return call_result.StartTransactionPayload(
                transaction_id=42, id_tag_info={"status": "Blocked"}
            )
        return await original(request)

    cp.call.side_effect = delay
    cp.state = Status.preparing
    await cp.on_remote_start_transaction("TAG", connector_id=1)
    await blocked.wait()
    cp.invalidate_pending_start()
    cp._shared_state.plugged_in = False
    cp.state = Status.available
    cp._shared_state.state = Status.available
    release.set()
    await asyncio.gather(*cp._background_tasks)
    assert cp.state == Status.available and cp._shared_state.state == Status.available
