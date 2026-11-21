from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Optional

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    ClearCacheStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnlockStatus,
    UpdateStatus,
)

from src.charger_sim import ChargerReading, ChargerSimulator
from src.charging_profile import ChargingProfileScheduler
from src.meter_values import build_charging_meter_values, build_idle_meter_values
from src.persistence import Persistence
from src.shared_state import SharedState

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ChargePoint(BaseChargePoint):
    def __init__(
        self,
        id: str,
        connection,
        persistence: Persistence,
        current_amps: int = 32,
        shared_state: SharedState | None = None,
    ):
        super().__init__(id, connection)
        self.connector_id = 1
        self.state = (
            ChargePointStatus.preparing
            if shared_state and shared_state.plugged_in
            else ChargePointStatus.available
        )
        self._persistence = persistence
        self._power_override: Optional[float] = None  # kW, set via API
        self._shared_state = shared_state or SharedState()
        self._energy_register_wh: int = persistence.load_energy_register_wh()
        self._transaction_id: Optional[int] = None
        self._transaction_start_energy_wh: int = 0
        self._last_meter_time: float = time.monotonic()
        self._last_power_kw = 0.0
        self._energy_remainder_wh = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._starting_transaction = False
        self._stopping_transaction = False
        self._pending_inoperative = False
        self._command_generation = 0
        self._shared_state.state = self.state
        self._shared_state.transaction_id = None
        self._shared_state.energy_kwh = self.energy_register_kwh
        self._shared_state.current_amps_setting = current_amps
        self._meter_value_interval: int = 60  # default, updated by ChangeConfiguration
        self._server_config: dict[str, str] = {}  # stores config sent by server
        self._local_list_version: int = 0
        self._local_auth_list: dict[str, dict] = {}
        self._config_store: dict[str, tuple[str, bool]] = {
            # key: (value, readonly)
            "MeterValueSampleInterval": ("60", False),
            "HeartbeatInterval": ("30", False),
            "NumberOfConnectors": ("1", True),
            "ChargeProfileMaxStackLevel": ("3", True),
            "ChargingScheduleAllowedChargingRateUnit": ("Current,Power", True),
            "ChargingScheduleMaxPeriods": ("24", True),
            "MaxChargingProfilesInstalled": ("5", True),
            "SupportedFeatureProfiles": (
                "Core,SmartCharging,LocalAuthListManagement,RemoteTrigger",
                True,
            ),
            "AuthorizeRemoteTxRequests": ("true", False),
            "LocalAuthListEnabled": ("true", False),
            "LocalAuthListMaxLength": ("20", True),
            "SendLocalListMaxLength": ("20", True),
            "StopTransactionOnInvalidId": ("false", False),
            "StopTransactionOnEVSideDisconnect": ("true", True),
            "UnlockConnectorOnEVSideDisconnect": ("true", True),
            "WebSocketPingInterval": ("30", True),
            "MeterValuesAlignedData": ("", False),
            "MeterValuesSampledData": (
                "Energy.Active.Import.Register,Power.Active.Import",
                False,
            ),
            "ConnectorPhaseRotation": ("1.RST", True),
            "GetConfigurationMaxKeys": ("50", True),
        }
        self._charger_sim = ChargerSimulator(current_amps=current_amps)
        self._profile_scheduler = ChargingProfileScheduler(
            rated_power_w=self._charger_sim.rated_power_kw * 1000
        )

    @property
    def energy_register_kwh(self) -> float:
        return self._energy_register_wh / 1000.0

    def _zero_power_state(self) -> None:
        """Zero out power-related shared state values."""
        self._last_power_kw = 0.0
        self._shared_state.power_kw = 0.0
        self._shared_state.current_a = 0.0
        self._shared_state.power_offered_kw = 0.0
        self._shared_state.power_source = "simulated"
        self._shared_state.power_entity_value = None

    async def send_boot_notification(
        self,
        model: str,
        vendor: str,
        serial_number: str = "",
        firmware_version: str = "",
    ) -> int:
        request = call.BootNotificationPayload(
            charge_point_model=model,
            charge_point_vendor=vendor,
            charge_point_serial_number=serial_number,
            charge_box_serial_number=serial_number,
            firmware_version=firmware_version,
            meter_type="Internal NON compliant",
            meter_serial_number="",
            iccid="",
            imsi="",
        )

        while True:
            response: call_result.BootNotificationPayload = await self.call(request)

            if response.status == RegistrationStatus.accepted:
                self._shared_state.connected_to_server = True
                await self._send_status_for_connector(0)
                await self.send_status()
                interval = response.interval
                interval = interval if interval and interval > 0 else 30
                self._config_store["HeartbeatInterval"] = (str(interval), False)
                return interval
            elif response.status == RegistrationStatus.pending:
                wait = (
                    response.interval
                    if response.interval and response.interval > 0
                    else 30
                )
                logger.info("BootNotification pending, retrying in %ds", wait)
                await asyncio.sleep(wait)
            else:
                logger.error("BootNotification rejected")
                raise SystemExit("BootNotification rejected by server")

    async def heartbeat_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(int(self._config_store["HeartbeatInterval"][0]))
            try:
                await self.call(call.HeartbeatPayload())
            except Exception:
                logger.warning("Heartbeat cycle failed", exc_info=True)

    async def meter_values_loop(self) -> None:
        while True:
            await asyncio.sleep(self._meter_value_interval)
            try:
                await self.send_meter_values()
            except Exception:
                logger.warning("Meter values cycle failed", exc_info=True)

    async def _send_status_for_connector(
        self, connector_id: int, status: ChargePointStatus | None = None
    ) -> None:
        """Send StatusNotification for a connector."""
        request = call.StatusNotificationPayload(
            connector_id=connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=status or self.state,
            timestamp=_now_iso(),
            info="",
            vendor_id="",
            vendor_error_code="",
        )
        await self.call(request)

    async def send_status(self) -> None:
        await self._send_status_for_connector(self.connector_id)

    async def send_status_unavailable(self) -> None:
        await self._send_status_for_connector(
            self.connector_id,
            ChargePointStatus.unavailable,
        )

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def finished(done):
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("OCPP follow-up task failed")

        task.add_done_callback(finished)
        return task

    def invalidate_pending_start(self):
        self._command_generation += 1

    async def shutdown(self):
        for task in list(self._background_tasks):
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._account_energy()
        self._charger_sim.stop_charging()
        self._shared_state.connected_to_server = False
        self._shared_state.state = ChargePointStatus.unavailable
        self._shared_state.transaction_id = None
        self._transaction_id = None
        self._zero_power_state()

    def _account_energy(self):
        now = time.monotonic()
        added = max(0, now - self._last_meter_time) * self._last_power_kw / 3.6
        self._last_meter_time = now
        accumulated = added + self._energy_remainder_wh
        whole = int(accumulated)
        self._energy_remainder_wh = accumulated - whole
        if whole:
            self._energy_register_wh += whole
            self._persistence.save_energy_register_wh(self._energy_register_wh)
        self._shared_state.energy_kwh = self.energy_register_kwh

    def _effective_reading(self, limit_kw):
        reading = self._charger_sim.sample()
        offered = (
            reading.power_offered_kw
            if limit_kw is None
            else min(reading.power_offered_kw, max(0, limit_kw))
        )
        power = min(reading.power_kw, offered)
        if self._power_override is not None:
            power = max(0, min(power, self._power_override))
        return ChargerReading(
            power,
            reading.voltage,
            round(power * 1000 / reading.voltage, 2),
            reading.frequency_hz,
            offered,
            min(reading.current_offered_a, offered * 1000 / reading.voltage),
        )

    async def send_meter_values(self) -> None:
        # Integrate the previous observed power only over its actual charging interval.
        self._account_energy()
        limit_kw = self._profile_scheduler.get_current_limit_kw()
        if self._transaction_id is not None:
            allowed = limit_kw is None or limit_kw > 0
            if allowed and self.state == ChargePointStatus.suspended_evse:
                self._charger_sim.start_charging()
                self.state = ChargePointStatus.charging
                self._shared_state.state = self.state
                await self.send_status()
            elif not allowed and self._charger_sim.is_charging:
                self._charger_sim.stop_charging()
                self.state = ChargePointStatus.suspended_evse
                self._shared_state.state = self.state
                self._zero_power_state()
                await self.send_status()

        if self.state == ChargePointStatus.charging:
            reading = self._effective_reading(limit_kw)
            self._last_power_kw = reading.power_kw
            self._shared_state.power_source = (
                "entity" if self._power_override is not None else "simulated"
            )
            self._shared_state.power_entity_value = self._power_override

            meter_value = build_charging_meter_values(
                reading=reading,
                energy_register_wh=self._energy_register_wh,
            )
            request = call.MeterValuesPayload(
                connector_id=self.connector_id,
                transaction_id=self._transaction_id,
                meter_value=meter_value,
            )

            # Update shared state with charging values
            self._shared_state.power_kw = reading.power_kw
            self._shared_state.voltage = reading.voltage
            self._shared_state.current_a = reading.current_a
            self._shared_state.frequency_hz = reading.frequency_hz
            self._shared_state.power_offered_kw = reading.power_offered_kw
            self._shared_state.energy_kwh = self.energy_register_kwh
        else:
            meter_value = build_idle_meter_values(
                energy_register_wh=self._energy_register_wh,
            )
            request = call.MeterValuesPayload(
                connector_id=self.connector_id,
                meter_value=meter_value,
            )
            self._shared_state.energy_kwh = self.energy_register_kwh
            # Bug 7 fix: zero out power values when idle
            self._zero_power_state()

        await self.call(request)

    async def _do_start_transaction(
        self, id_tag="NoAuthorization", generation=None
    ) -> None:
        """Start a charging transaction; settle a late accepted start after cancellation."""
        if generation is None:
            generation = self._command_generation
        try:
            if generation != self._command_generation:
                return
            # Send OCPP protocol states but don't update shared_state with
            # intermediate values (Bug 1 fix) — the API should only see the
            # final stable state (Charging).
            self.state = ChargePointStatus.available
            await self.send_status()
            await self._send_status_for_connector(0)

            self.state = ChargePointStatus.suspended_ev
            await self.send_status()

            self._transaction_start_energy_wh = self._energy_register_wh
            request = call.StartTransactionPayload(
                connector_id=self.connector_id,
                id_tag=id_tag,
                meter_start=self._energy_register_wh,
                timestamp=_now_iso(),
            )
            response: call_result.StartTransactionPayload = await self.call(request)
            if response.id_tag_info.get("status") != "Accepted":
                raise ValueError("Transaction authorization rejected")
            self._transaction_id = response.transaction_id
            if generation != self._command_generation:
                final = (
                    ChargePointStatus.preparing
                    if self._shared_state.plugged_in
                    else ChargePointStatus.available
                )
                await self._do_stop_transaction(final_state=final)
                return
            self._charger_sim.start_charging()
            self._last_meter_time = time.monotonic()
            self._profile_scheduler.start_transaction()
            limit_kw = self._profile_scheduler.get_current_limit_kw()
            self._last_power_kw = self._effective_reading(limit_kw).power_kw
            logger.info("Transaction started: %s", self._transaction_id)

            # Transition to Charging — now update shared_state atomically
            self.state = ChargePointStatus.charging
            if limit_kw is not None and limit_kw <= 0:
                self._charger_sim.stop_charging()
                self.state = ChargePointStatus.suspended_evse
            self._shared_state.state = self.state
            self._shared_state.transaction_id = self._transaction_id
            await self.send_status()
        except Exception:
            # Bug 2 fix: reset to consistent state on failure
            logger.error("Failed to start transaction", exc_info=True)
            self._charger_sim.stop_charging()
            self._transaction_id = None
            if generation == self._command_generation:
                self.state = ChargePointStatus.preparing
            else:
                self.state = self._shared_state.state
            self._shared_state.state = self.state
            self._shared_state.transaction_id = None
            self._zero_power_state()

        finally:
            self._starting_transaction = False

    async def _do_stop_transaction(
        self,
        final_state: ChargePointStatus = ChargePointStatus.preparing,
    ) -> None:
        """Stop the active transaction. final_state controls where we end up."""
        try:
            transaction_id = self._transaction_id
            if transaction_id is None:
                return
            self._account_energy()
            self._last_power_kw = 0.0
            self._charger_sim.stop_charging()
            self._zero_power_state()
            self._profile_scheduler.clear_profile(purpose="TxProfile")

            # Send Finishing status before StopTransaction
            self.state = ChargePointStatus.finishing
            await self.send_status()

            request = call.StopTransactionPayload(
                transaction_id=transaction_id,
                id_tag="ffffffffffffff7f",
                meter_stop=self._energy_register_wh,
                timestamp=_now_iso(),
                reason=Reason.remote,
                transaction_data=[],
            )
            response: call_result.StopTransactionPayload = await self.call(request)
            logger.info(
                "Transaction %s stopped, response: %s", transaction_id, response
            )
        except Exception:
            logger.error("Failed to stop transaction", exc_info=True)
        finally:
            self._transaction_id = None
            self._shared_state.transaction_id = None
            self._zero_power_state()
            self._stopping_transaction = False
            if self._pending_inoperative:
                final_state = ChargePointStatus.unavailable
                self._pending_inoperative = False
            self.state = final_state
            self._shared_state.state = self.state
            if not asyncio.current_task().cancelling():
                try:
                    await self.send_status()
                except Exception:
                    logger.warning("Failed to send final transaction status")

    @on(Action.RemoteStartTransaction)
    async def on_remote_start_transaction(
        self, id_tag: str, charging_profile: dict = None, **kwargs
    ):
        # Bug 6 fix: only accept when car is plugged in (Preparing)
        if (
            self._starting_transaction
            or self._stopping_transaction
            or self._transaction_id is not None
            or kwargs.get("connector_id", 1) != 1
            or self.state
            not in (
                ChargePointStatus.preparing,
                ChargePointStatus.suspended_ev,
                ChargePointStatus.suspended_evse,
            )
        ):
            logger.warning(
                "RemoteStart rejected: state is %s (not plugged in)", self.state
            )
            return call_result.RemoteStartTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )

        if charging_profile:
            try:
                self._profile_scheduler.set_profile(charging_profile)
            except (ValueError, TypeError, KeyError):
                return call_result.RemoteStartTransactionPayload(
                    status=RemoteStartStopStatus.rejected
                )

        self._shared_state.plugged_in = True
        self._starting_transaction = True
        self._spawn(self._do_start_transaction(id_tag, self._command_generation))
        return call_result.RemoteStartTransactionPayload(
            status=RemoteStartStopStatus.accepted
        )

    @on(Action.RemoteStopTransaction)
    async def on_remote_stop_transaction(self, transaction_id: int, **kwargs):
        if self._transaction_id is None or self._stopping_transaction:
            logger.warning("RemoteStop rejected: no active transaction")
            return call_result.RemoteStopTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )
        if transaction_id != self._transaction_id:
            logger.warning(
                "RemoteStop rejected: transaction_id %s != current %s",
                transaction_id,
                self._transaction_id,
            )
            return call_result.RemoteStopTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )
        self._stopping_transaction = True
        self._spawn(self._do_stop_transaction())
        return call_result.RemoteStopTransactionPayload(
            status=RemoteStartStopStatus.accepted
        )

    @on(Action.GetConfiguration)
    async def on_get_configuration(self, key: list[str] = None, **kwargs):
        """Return configuration keys. Returns all keys if none specified."""
        configuration_key = []
        unknown_key = []

        keys_to_return = key if key else list(self._config_store.keys())

        for k in keys_to_return:
            if k in self._config_store:
                value, readonly = self._config_store[k]
                configuration_key.append(
                    {
                        "key": k,
                        "readonly": readonly,
                        "value": value,
                    }
                )
            else:
                unknown_key.append(k)

        return call_result.GetConfigurationPayload(
            configuration_key=configuration_key,
            unknown_key=unknown_key,
        )

    @on(Action.ChangeConfiguration)
    async def on_change_configuration(self, key: str, value: str, **kwargs):
        """Handle configuration changes from the server."""
        if key not in self._config_store and key != "chargingALimitConn1":
            return call_result.ChangeConfigurationPayload(
                status=ConfigurationStatus.not_supported
            )
        if key in self._config_store and self._config_store[key][1]:
            return call_result.ChangeConfigurationPayload(
                status=ConfigurationStatus.rejected
            )
        try:
            if key in ("MeterValueSampleInterval", "HeartbeatInterval"):
                interval = int(value)
                if interval < 1:
                    raise ValueError("interval must be >= 1")
            if key == "chargingALimitConn1":
                amps = int(value)
                self._charger_sim.current_amps = amps
                self._shared_state.current_amps_setting = amps
        except (ValueError, TypeError):
            return call_result.ChangeConfigurationPayload(
                status=ConfigurationStatus.rejected
            )
        if key in self._config_store:
            self._config_store[key] = (value, False)
        self._server_config[key] = value
        self._shared_state.server_config = dict(self._server_config)
        if key == "MeterValueSampleInterval":
            self._meter_value_interval = interval
            self._shared_state.meter_interval = interval
        return call_result.ChangeConfigurationPayload(
            status=ConfigurationStatus.accepted
        )

    @on(Action.GetLocalListVersion)
    async def on_get_local_list_version(self, **kwargs):
        """Return the current local authorization list version."""
        return call_result.GetLocalListVersionPayload(
            list_version=self._local_list_version
        )

    @on(Action.SendLocalList)
    async def on_send_local_list(
        self,
        list_version: int,
        update_type: str,
        local_authorization_list: list[dict] = None,
        **kwargs,
    ):
        """Handle local authorization list updates."""
        logger.info(
            "SendLocalList: version=%d type=%s entries=%d",
            list_version,
            update_type,
            len(local_authorization_list) if local_authorization_list else 0,
        )

        if update_type == "Full":
            self._local_auth_list.clear()

        if local_authorization_list:
            for entry in local_authorization_list:
                id_tag = entry.get("id_tag", entry.get("idTag", ""))
                if id_tag:
                    self._local_auth_list[id_tag] = entry

        self._local_list_version = list_version
        return call_result.SendLocalListPayload(status=UpdateStatus.accepted)

    @on(Action.SetChargingProfile)
    async def on_set_charging_profile(
        self, connector_id: int, cs_charging_profiles: dict, **kwargs
    ):
        logger.info("SetChargingProfile received for connector %s", connector_id)
        try:
            transaction = cs_charging_profiles.get(
                "transaction_id", cs_charging_profiles.get("transactionId")
            )
            if transaction is not None and transaction != self._transaction_id:
                raise ValueError("Profile transaction mismatch")
            self._profile_scheduler.set_profile(cs_charging_profiles, connector_id)
        except (ValueError, TypeError, KeyError):
            return call_result.SetChargingProfilePayload(status="Rejected")
        return call_result.SetChargingProfilePayload(status="Accepted")

    @on(Action.ClearChargingProfile)
    async def on_clear_charging_profile(self, **kwargs):
        logger.info("ClearChargingProfile received: %s", kwargs)
        matched = self._profile_scheduler.clear_profile(
            profile_id=kwargs.get("id"),
            connector_id=kwargs.get("connector_id"),
            purpose=kwargs.get("charging_profile_purpose"),
            stack_level=kwargs.get("stack_level"),
        )
        return call_result.ClearChargingProfilePayload(
            status="Accepted" if matched else "Unknown"
        )

    @on(Action.TriggerMessage)
    async def on_trigger_message(
        self, requested_message: str, connector_id: int = 0, **kwargs
    ):
        """Handle TriggerMessage: server requests the CP to send a specific message."""
        supported = {
            "BootNotification",
            "Heartbeat",
            "MeterValues",
            "StatusNotification",
        }
        if requested_message not in supported:
            logger.warning(
                "TriggerMessage for unsupported message: %s", requested_message
            )
            return call_result.TriggerMessagePayload(
                status=TriggerMessageStatus.not_implemented
            )

        logger.info("TriggerMessage accepted: %s", requested_message)
        self._spawn(self._handle_triggered_message(requested_message, connector_id))
        return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)

    async def _handle_triggered_message(self, message: str, connector_id: int) -> None:
        """Execute the triggered message after returning the response."""
        try:
            if message == "StatusNotification":
                await self._send_status_for_connector(connector_id or self.connector_id)
            elif message == "MeterValues":
                await self.send_meter_values()
            elif message == "Heartbeat":
                await self.call(call.HeartbeatPayload())
            elif message == "BootNotification":
                await self.call(
                    call.BootNotificationPayload(
                        charge_point_model="",
                        charge_point_vendor="",
                    )
                )
        except Exception:
            logger.warning("Failed to send triggered %s", message, exc_info=True)

    @on(Action.ChangeAvailability)
    async def on_change_availability(self, connector_id: int, type: str, **kwargs):
        """Handle ChangeAvailability: server requests connector operative/inoperative."""
        logger.info("ChangeAvailability: connector=%d type=%s", connector_id, type)

        if connector_id not in (0, self.connector_id):
            return call_result.ChangeAvailabilityPayload(
                status=AvailabilityStatus.rejected
            )
        if type == AvailabilityType.inoperative:
            if self._transaction_id is not None:
                self._pending_inoperative = True
                logger.info(
                    "Transaction active, will change availability after completion"
                )
                return call_result.ChangeAvailabilityPayload(
                    status=AvailabilityStatus.scheduled
                )
            self.state = ChargePointStatus.unavailable
            self._shared_state.state = self.state
            self._spawn(self.send_status())
        else:
            self._pending_inoperative = False
            if self.state == ChargePointStatus.unavailable:
                self.state = ChargePointStatus.available
                self._shared_state.state = self.state
                self._spawn(self.send_status())

        return call_result.ChangeAvailabilityPayload(status=AvailabilityStatus.accepted)

    @on(Action.UnlockConnector)
    async def on_unlock_connector(self, connector_id: int, **kwargs):
        """Handle UnlockConnector: server requests to unlock a connector."""
        logger.info("UnlockConnector: connector=%d", connector_id)

        if connector_id != self.connector_id:
            return call_result.UnlockConnectorPayload(status=UnlockStatus.not_supported)

        self.invalidate_pending_start()
        if self._transaction_id is not None:
            self._spawn(
                self._do_stop_transaction(final_state=ChargePointStatus.available)
            )

        self._shared_state.plugged_in = False
        self.state = ChargePointStatus.available
        self._shared_state.state = self.state

        return call_result.UnlockConnectorPayload(status=UnlockStatus.unlocked)

    @on(Action.Reset)
    async def on_reset(self, type: str, **kwargs):
        """Handle Reset: server requests a hard or soft reset."""
        logger.info("Reset requested: type=%s", type)

        self.invalidate_pending_start()
        self._spawn(self._reset(type))
        return call_result.ResetPayload(status=ResetStatus.accepted)

    async def _reset(self, reset_type):
        if self._transaction_id is not None:
            await self._do_stop_transaction(final_state=ChargePointStatus.available)
        self._profile_scheduler.clear_profile()
        self.state = ChargePointStatus.available
        self._shared_state.state = self.state
        self._shared_state.plugged_in = False
        self._zero_power_state()
        if reset_type == ResetType.hard:
            await asyncio.sleep(2)
            await self._connection.close(code=1012, reason="Charger reset")
        else:
            await self.send_status()

    @on(Action.ClearCache)
    async def on_clear_cache(self, **kwargs):
        """Handle ClearCache: server requests clearing the authorization cache."""
        logger.info("ClearCache requested")
        return call_result.ClearCachePayload(status=ClearCacheStatus.accepted)

    @on(Action.DataTransfer)
    async def on_data_transfer(
        self, vendor_id: str, message_id: str = "", data: str = "", **kwargs
    ):
        """Handle DataTransfer: vendor-specific messaging."""
        logger.info(
            "DataTransfer: vendor=%s message_id=%s data_len=%d",
            vendor_id,
            message_id,
            len(data) if data else 0,
        )
        return call_result.DataTransferPayload(
            status=DataTransferStatus.unknown_vendor_id,
            data="",
        )
