# Transparent relay

Select `mode: relay` to relay a real Wallbox connection to the configured OCPP
provider. `emulator` remains the default. Modes are mutually exclusive. Relay mode
never simulates a charger, calculates a target, injects meter readings, acknowledges
profiles itself, or replays commands. WebSocket text/binary message contents pass
unchanged; framing, ping/pong and TLS terminate separately on each connection.

```text
Wallbox <-- authenticated WS/WSS --> relay <-- verified WSS --> Octopus
                                      |
                             redacted diagnostic logs
                             read-only HA API and UI
```

## Configuration

Use the existing `server_hostname` (hostname and optional port without a scheme
or path), `chargepoint_id` and `password`. The relay authenticates the charger
using the same HTTP Basic username/password. Only one charger is accepted.

Set `mode: relay` and map app TCP port **9000** to a host port. The charger endpoint
is `ws://<HA-LAN-IP>:9000/<chargepoint_id>`, using OCPP 1.6J and the original
credentials. Some charger setup screens append the ID themselves: include it
exactly once in the resulting full path, without a query string. Ingress and port
8099 are diagnostic interfaces, not charger endpoints.

For WSS, set both `relay_certfile` and `relay_keyfile` to certificate-chain and
private-key paths under `/ssl`; use `wss://<certificate-hostname>:9000/<id>`.
The charger must trust the certificate. Leaving both empty enables plaintext WS:
use that only on a trusted LAN, since Basic credentials travel in clear. Do not
expose the listener or internal diagnostic API to the internet. Upstream uses
verified TLS; there is no certificate-verification bypass. Standalone tests can
set `IO_USE_TLS=false` for their local simulated provider.

Model, serial, firmware, current, initial-energy and HA power-entity options are
ignored by the relay. They remain available for emulator mode.

## Evidence

Ingress shows connection state, message count, lost-log count and the last 100
records. Existing companion sensors read observed charger measurements; unknown
measurements stay unknown. Simulator controls become unavailable and all API
write requests return 405.

Logs persist in `/data/relay/traffic.jsonl`, rotating at approximately 5 MiB with
five backups (approximately 30 MiB plus one oversized record). Forwarded-message
records include UTC time, session ID, direction, message ID/type, CALL action,
original-message SHA-256 and a redacted parsed copy. A record means the message
was sent to the peer's WebSocket; a matching CALLRESULT or CALLERROR records the
peer's actual response. Malformed/binary messages retain size and hash only.
Common credential fields, credential configuration values and download locations
are redacted. Logs can still contain charger identifiers and other personal data;
review before sharing. WebSocket transport DEBUG output is suppressed in relay mode.

Inspect `SetChargingProfile` and its actual response: profile ID, stack level,
purpose, validity, start, duration and periods. Also inspect `ClearChargingProfile`,
`RemoteStartTransaction`, `RemoteStopTransaction`, `StatusNotification`,
`StartTransaction`, `StopTransaction` and `MeterValues`. Correlate responses by
message ID within the same session. This can show whether an earlier schedule
remained after a target/departure update and whether the Wallbox accepted changes.

The write queue holds 256 records; slow/full storage drops records rather than
blocking charging. `log_dropped` and `log_error` expose capture gaps. Recent traffic
holds 100 records; individual WebSocket messages are limited to 1 MiB. There is
no durable command buffering. Either connection ending closes its partner; an
upstream outage closes the charger connection so the Wallbox can reconnect.
The relay cannot guarantee that the charger stops an accepted offline schedule
during an outage.

## First live test and rollback

Automated tests use local simulated providers, never Octopus. Real Wallbox and
Octopus compatibility still needs verification. Deployment requires access to
the charger credentials and the ability to configure its OCPP endpoint. If an
app-managed integration prevents that, it is a deployment blocker.

Preserve the original full charger endpoint, ID, credentials and connection
settings before changing anything. Stop any emulator using the same identity.
Start the relay, then change only the charger endpoint. Verify both connections,
the real BootNotification response and profile responses before a supervised
charge. Keep charge-target automations unchanged during the initial capture.
Rollback restores the original charger endpoint/settings and stops the relay.
The implementation never changes charger settings automatically.

## Development

From `ocpp_charge_proxy/`:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Tests cover exact message contents, real profile responses, handshake rejection,
TLS verification, single-charger admission, disconnect/reconnect without replay,
read-only API, observed telemetry, log redaction, rotation and failed storage.
