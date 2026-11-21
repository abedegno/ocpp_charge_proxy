# OCPP Charge Proxy app

Choose `emulator` for a virtual single-phase chargepoint or `relay` to observe a
real charger's OCPP 1.6J conversation. The emulator remains the default. Read
[relay setup](https://github.com/abedegno/ocpp_charge_proxy/blob/master/ocpp_charge_proxy/RELAY.md)
before changing a physical charger endpoint.

## Configuration

| Option | Default | Applies to | Meaning |
|--------|---------|------------|---------|
| `mode` | `emulator` | Both | `emulator` or `relay` |
| `server_hostname` | Required | Both | Provider host and optional port, without scheme or path |
| `chargepoint_id` | Required | Both | Provider-assigned chargepoint identity / Basic username |
| `password` | Required | Both | Provider-assigned OCPP password |
| `relay_certfile` | Empty | Relay | Certificate chain under `/ssl`; requires matching key |
| `relay_keyfile` | Empty | Relay | TLS private key under `/ssl` |
| `charger_model` | `PLP2-0-2-2` | Emulator | Model reported in BootNotification |
| `charger_vendor` | `Wall Box Chargers` | Emulator | Reported vendor |
| `charger_serial` | Auto-generated | Emulator | Persisted virtual serial if not supplied |
| `firmware_version` | `6.11.16` | Emulator | Reported identity string, not this app's version |
| `current_amps` | `32` | Emulator | Virtual setting: 6/10/13/16/20/25/32 A |
| `initial_energy_wh` | `0` | Emulator | One-time register seed in Wh, cleared after reading |
| `log_level` | `info` | Both | `debug`, `info`, `warning`, `error` |

The app connects upstream over verified WSS. The standalone test runner can use
`--no-tls` for a local mock provider; this does not disable certificate verification
for WSS. Do not run an emulator and a real charger concurrently with the same
provider identity.

## Network ports and ingress

- **8099/TCP:** internal HTTP state/control API and ingress status page. Leave it
  unmapped on HA OS. It has no independent authentication.
- **9000/TCP:** authenticated charger-facing relay listener; map a host port only
  when using relay mode. It is not the ingress URL.
- With no relay certificate/key, the listener uses plaintext WS on a trusted LAN.
  With both configured, it uses WSS; the charger must trust the certificate.

## Emulator behavior

The app registers the configured identity, sends heartbeats and meter values, and
reports a virtual connector. The companion's Plugged In switch sets Preparing.
RemoteStartTransaction starts a virtual transaction using the requested ID tag;
RemoteStopTransaction stops it. Hard reset closes the connection for reconnection.
All work for a connection is cancelled before reconnecting, including command
follow-ups. An interrupted virtual transaction is not durably resumed.

Positive profile limits cap simulated/override power and offered power. Zero
limits suspend an active transaction; clearing or expiring a limit allows it to
resume. Absolute/Relative profiles honor validity, duration and applicable stack
levels. ChargePointMaxProfile caps transaction/default profiles. Recurring and
multiphase profiles are not implemented and are rejected. Profile boundaries are
checked during meter sampling, so timing precision depends on the meter interval.

Estimated energy integrates the last sampled power over charging time, excludes
pre-transaction idle time and includes the final interval at stop. The import
register persists atomically under `/data`; invalid files fall back to zero.
Simulation cannot reproduce a real vehicle's taper, losses or billing meter.

To control a physical load from emulator state, create separate HA automations.
This app does not turn the real Wallbox on or off in emulator mode.

## Companion integration

Copy `custom_components/ocpp_charge_proxy` from the repository to
`/config/custom_components/ocpp_charge_proxy`, then restart Core. Accept Supervisor
discovery or configure the internal API URL under Devices & Services. Keep the
configuration entry during upgrades to preserve entity IDs. Do not use `localhost`
to address a separate HA OS app container.

The combined app/integration repository cannot be installed through current
Marketplace rules. A Marketplace distribution requires a separate integration
repository; manual installation retains the existing architecture.

The optional power sensor supports recognized HA power units, converted to kW.
Values are capped by charger/profile limits. Unknown, unavailable, non-finite and
unsupported-unit values clear the override and restore simulation. No configured
sensor also means simulation. In relay mode this option is ignored, simulator
controls are unavailable, and API writes return 405.

## Relay diagnostics and rollback

Ingress shows both connection states and redacted recent traffic. Persistent,
rotating logs are in `/data/relay/traffic.jsonl`. Check `log_dropped` and `log_error`
before interpreting a capture. Matching OCPP responses show the real charger's
acknowledgement; forwarding alone does not prove a command was applied.

Save the original charger endpoint, identity and authentication before testing.
Rollback restores those settings and stops the relay. A charger may continue a
previously accepted offline schedule when the relay/provider is unavailable.

## Troubleshooting

- **Provider connection fails:** verify the OCPP hostname, identity, password,
  clock/TLS trust and that the same identity is not connected elsewhere.
- **Companion cannot connect:** check app status and use its internal container
  hostname with port 8099. Ensure Core can resolve/reach it.
- **Relay charger rejected:** check the full `/<chargepoint_id>` path, Basic
  credentials and `ocpp1.6` subprotocol; queries and other charger IDs are rejected.
- **Missing relay measurements:** aggregate Raw MeterValues must be reported by
  the real charger. The app does not invent missing values.
- **Missing logs:** check dropped-record/error indicators and available storage.

Provider accounts do not always expose OCPP credentials or endpoint settings.
The app cannot derive them from a consumer API integration.
