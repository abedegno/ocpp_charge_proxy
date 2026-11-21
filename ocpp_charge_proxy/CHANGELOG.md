# Changelog

## 0.2.0

- Add opt-in authenticated transparent OCPP relay; preserve emulator default.
- Forward real charger/provider messages unchanged, including profile acknowledgements.
- Add bounded rotating redacted JSONL logs and read-only ingress traffic viewer.
- Couple connection failures; reconnect through the charger without replay.
- Disable emulator API controls and power injection in relay mode.
- Support listener TLS and retain verified provider TLS.
- Modernize companion options flow and document Home Assistant 2026.11 installation.

- Fix emulator profile power caps, pause/clear/resume, validity/duration and purpose/connector matching.
- Reject unsupported profiles and invalid configuration instead of silently acknowledging them.
- Fix transaction energy accounting around idle/start/stop and slow network responses.
- Preserve requested ID tags; prevent duplicate and stale transaction starts after unplug/reset.
- Cancel connection-owned loops/follow-ups on reconnect and clear stale transaction state.
- Avoid OCPP handler deadlocks and send final stop/availability state to the provider.
- Convert HA sensor power units correctly, reject non-finite readings and close HTTP responses.
- Validate API inputs and persist energy/serial files with atomic replacement.
- Add actual Home Assistant companion and both-mode runtime tests; record coverage in CI.
- Gate container publishing on tests and refresh installation, limits and release documentation.

- Update app base images from end-of-support Alpine 3.21 to Alpine 3.24.

## 0.1.0

- Initial release
- OCPP 1.6 chargepoint proxy for smart tariff providers
- Initial OCPP handlers (not a full compliance claim): BootNotification, Heartbeat, MeterValues,
  StatusNotification, RemoteStart/StopTransaction, SetChargingProfile,
  ClearChargingProfile, GetConfiguration, ChangeConfiguration, TriggerMessage,
  ChangeAvailability, UnlockConnector, Reset, ClearCache, SendLocalList,
  GetLocalListVersion, DataTransfer
- Configurable charger identity (vendor, model, serial, firmware)
- Realistic power delivery simulation
- Power entity support: report real power from any HA sensor
- Companion HA integration with entities for automations and energy dashboard
- Ingress web UI for status monitoring
- Included AppArmor and ingress (no independently verified security-score claim)
