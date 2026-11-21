# OCPP Charge Proxy

An OCPP 1.6J app with two modes, plus a Home Assistant companion integration.
The app runs separately from Home Assistant Core; the integration exposes its
state as entities. Both components remain in this repository.

| Mode | Connection | Purpose |
|------|------------|---------|
| `emulator` (default) | Virtual chargepoint → provider | Simulate a single-phase charger and expose its state for automations |
| `relay` | Real charger ↔ app ↔ provider | Forward unchanged OCPP messages and inspect scheduling commands/responses |

Relay mode records the real charger's behavior. It does not change charge targets,
replace Octopus scheduling, generate meter readings or acknowledge commands on the
charger's behalf. See [relay setup and diagnostics](ocpp_charge_proxy/RELAY.md).

Emulator mode presents a virtual charger. An automation must control any real
charger or other load separately. Its energy readings are simulated estimates,
including when a power-entity override is used; they are not a billing meter.

## Compatibility

- OCPP **1.6J**, HTTP Basic charger authentication, one connector, single phase.
- Home Assistant OS app packaging: `aarch64` and `amd64`.
- Companion integration targets Home Assistant **2026.11+**; tests use 2026.11.1.
- The app uses Python 3.13 independently of Core; CI tests it on 3.13 and 3.14.
- Local simulated-provider tests cover both modes. Real Octopus/Wallbox relay
  compatibility still needs a supervised live test.

Provider credentials and the ability to configure an OCPP endpoint are required.
An account connected through a provider's consumer API does not necessarily expose
these credentials or allow changing the charger endpoint. Compatibility with every
provider, charger or EV is not implied.

## Install the app

1. Add `https://github.com/abedegno/ocpp_charge_proxy` under
   **Settings → Apps → App Store → ⋮ → Repositories**.
2. Install **OCPP Charge Proxy**. Read the app's Documentation tab.
3. Configure `server_hostname`, `chargepoint_id` and `password`, then choose a mode.
4. For relay mode, follow [the relay guide](ocpp_charge_proxy/RELAY.md) before
   changing the charger endpoint. Preserve the original settings for rollback.
5. Start the app and open its ingress page to check status.

App updates require a published container image matching `config.yaml`'s version.
A development branch is not an installed release. See [contributing](CONTRIBUTING.md)
for local builds and release checks.

## Install the companion integration

The app's ingress page works without the integration. For HA entities:

1. Copy `custom_components/ocpp_charge_proxy` into
   `/config/custom_components/ocpp_charge_proxy` and restart Home Assistant.
2. Accept Supervisor discovery, or add **OCPP Charge Proxy** under
   **Settings → Devices & Services** and enter the internal app API URL.
3. Keep the configuration entry when replacing the integration files so existing
   entity IDs remain stable. Restart Core after each manual update.

The internal API listens on port 8099. Supervisor discovery supplies the container
hostname. `localhost` in Home Assistant Core does not refer to a separate app
container. Standalone installations need an API URL reachable from Core.

The current [Marketplace repository rules](https://contributors.home-assistant.io/marketplace/reference/repository-structure/)
exclude repositories containing Home Assistant apps. This combined repository
therefore uses manual companion installation; a Marketplace distribution would
need an integration-only repository. This packaging restriction does not require
removing either runtime component.

## Entities and controls

| Entity | Emulator | Relay |
|--------|----------|-------|
| State | Virtual connector state | Last observed connector-1 state |
| Power / Current | Simulated or capped power override | Observed aggregate meter values; unknown until reported |
| Energy | Persisted estimated import register | Observed import register; not session energy |
| Power Source | `simulated` or `entity` | `wallbox` |
| Connected to Server | Provider connection | Provider connection |
| Plugged In switch | Simulate plug/unplug | Unavailable |
| Current Amps selector | Virtual current setting | Unavailable |

The optional power sensor applies only to emulator mode. Supported HA power units
are converted to kW. Unknown, unavailable, non-finite or unsupported-unit readings
clear the override and resume simulation. Relay mode never pushes power overrides.

## Scope and limitations

The emulator evaluates single-phase Absolute and Relative profiles, stack levels,
profile validity and duration, and combines transaction limits with chargepoint
maximum limits. Unsupported recurring/multiphase profiles are rejected. Profile
state changes are evaluated on meter cycles, not by an exact real-time scheduler.
Energy uses the last sampled power between transitions. The emulator is a test and
automation tool, not a certified, complete OCPP implementation. Local authorization
list storage is not an access-control system.

The relay requires the original charger identity and credentials. It changes the
network path, so connection interruptions remain possible. Previously accepted
charger schedules may continue offline. Preserve the direct endpoint for rollback.
The internal API has no separate authentication: leave port 8099 unmapped on HA OS
and do not expose it publicly. Use TLS for the charger-facing listener where possible.

## Development and documentation

- [App configuration and emulator behavior](ocpp_charge_proxy/DOCS.md)
- [Relay setup, logs and rollback](ocpp_charge_proxy/RELAY.md)
- [Tests, coverage and release process](CONTRIBUTING.md)
- [Review findings and remaining limits](REVIEW.md)
- [Changelog](ocpp_charge_proxy/CHANGELOG.md)
- [MIT license](LICENSE.md)
