# Repository review — 21 November 2026

Reviewed public base commit `2b36526` and the prepared 0.2.0 relay extension.
This review covers code, regression coverage, app/companion boundaries, CI and user
and contributor documentation. It is not a certification of OCPP compliance or a
live Octopus/Wallbox interoperability test.

## Findings fixed

| Area | Reproduced problem | Change and verification |
|------|--------------------|-------------------------|
| Emulator scheduling | Positive limits were ignored; clearing a zero-limit profile never resumed charging | Cap delivered/offered power and current; resume when no effective restriction remains |
| Profile lifetime | Duration/validity ignored; purposes collided at the same stack level; clear filters ignored connector/combined criteria | Evaluate bounds, purpose/connector stacks and maximum caps; combine clear criteria; reject unsupported profiles |
| Meter accounting | Idle time counted as charging; final interval omitted; slow stop could accumulate phantom energy | Account last power at transitions, retain fractional Wh, stop accounting before network waits |
| Connection ownership | Old heartbeat/meter loops survived receiver failure | Own setup, receiver, periodic and follow-up tasks per connection; cancel and await teardown |
| Protocol routing | ChangeAvailability/Reset blocked the receive loop waiting for their own replies | Return command response and perform outbound work separately; tests use the actual OCPP routing loop |
| Transaction races | Duplicate starts and late accepted/rejected starts after unplug could restore charging/preparing | Reserve pending starts and invalidate generations; settle late accepted transactions with StopTransaction |
| Reconnection/state | API retained stale Charging/transaction ID; final stop state stayed local | Clear stale session data, preserve virtual plug intent and send final provider status |
| Configuration | Invalid numeric configuration was acknowledged/stored; invalid startup values failed late | Validate before committing; return rejection/not-supported; reject malformed endpoints/current settings early |
| Companion units | All power sensors divided by 1,000, including kW readings; NaN/infinity forwarded | Convert recognized HA units to kW and clear invalid overrides; close HTTP responses |
| API | Fractional currents were truncated; missing/invalid/non-finite power could mutate state | Require integer current and explicit finite numeric/null power; relay writes remain blocked |
| Persistence | Valid JSON of wrong shape/type could crash startup; interrupted writes could truncate a register | Validate reads and use fsync plus atomic replacement |
| Relay diagnostics | Stalled setup retained disconnected chargers; download URLs leaked credentials; recursive payload inspection could interrupt forwarding | Couple setup cancellation, redact locations and fall back to hash-only inspection; independent review regressions |
| Build/CI | Image builds ran independently of tests; shallow/multi-commit change detection was unreliable | Reusable app/companion tests gate builds; compare event base and parse filenames as data |
| Base image | Alpine 3.21 support ended on 1 November 2026 | Move both architecture definitions to available Alpine 3.24 images; verify builds |

## Coverage evidence

The unchanged public base passes **52 app tests**, with **49%** combined line/branch
coverage and no companion runtime suite. The refreshed app passes **124 tests**
with **77%** combined line/branch coverage. The companion passes **13 tests** using
Home Assistant **2026.11.1** classes, with **74%** combined coverage. These are
local measurements, not a claim that all behavior is covered. Six additional
workflow-metadata regression cases cover quoted helper outputs, unavailable
architectures and PR-versus-release behavior. Both refreshed
Alpine 3.24 images build locally and pass all 124 app tests inside their respective
aarch64/amd64 Python 3.13 containers (77.01% combined coverage).

Coverage includes raw bidirectional relay contents, profile acknowledgements,
TLS trust rejection, handshake validation, disconnect/reconnect and logging
failures; emulator protocol routing and task cleanup; start/stop timing races;
actual app entry points; HA power units, relay control availability, options flow,
entity identity and setup/unload behavior. Regression failures were observed
before the corresponding fixes.

CI runs app tests on Python 3.13 and 3.14, and companion tests on Python 3.14 with
the pinned HA runtime. It records coverage and enforces a 70% app floor. GitHub
workflow syntax/shell expressions are checked locally with actionlint.

## Documentation corrections

Rewrote the root/app README, app documentation and contributor guide, and updated
the relay guide, changelog, configuration labels and build metadata. Installation
uses current Apps terminology and manual companion installation for this mixed
repository. Documented Marketplace packaging restrictions, release/image
requirements, ports, TLS, credentials, unit conversion/fallback, both modes,
rollback and simulator limitations. Removed unsupported universal-compatibility,
complete-OCPP and fixed-security-score claims.

References checked:

- [Current Marketplace repository rules](https://contributors.home-assistant.io/marketplace/reference/repository-structure/)
- [Current options-flow lifecycle](https://contributors.home-assistant.io/core/integrations/options-flow/)
- [Home Assistant 2026.11.1 runtime/dependencies](https://github.com/home-assistant/core/blob/2026.11.1/pyproject.toml)
- [Alpine support dates](https://alpinelinux.org/releases/)

## Remaining verification and limits

- Real Octopus/Wallbox relay behavior, credential access and endpoint editing
  require a supervised live test. This work does not change a live charger.
- A full disposable HA OS/Supervisor installation, discovery/upgrade path and
  Marketplace distribution are not covered by the local companion tests.
- Emulator profiles are single-phase Absolute/Relative; recurring/multiphase
  profiles are rejected. Profile timing follows meter cycles. Energy estimates
  are not billing measurements. Interrupted transactions are not durably resumed.
- Local authorization lists are storage, not enforced charger access control.
  The emulator is not a complete OCPP implementation.
- The internal HTTP API is unauthenticated; keep it private/unmapped. Redacted
  relay captures still include device/session metadata. Accepted offline charger
  schedules may continue during a relay/provider outage.
- Manual/discovery config-flow branches, interactive console and several emulator
  error/vendor-command paths remain coverage gaps. Review coverage artifacts
  before extending those behaviors; a passing suite does not establish their
  completeness.

- The pinned HA builder still accepts `build.yaml`, but CI flags that file as
  deprecated. Moving base-image selection and build metadata entirely into the
  Dockerfile remains a build-toolchain follow-up; both current architecture builds
  pass. Deprecated Node 20 setup/artifact actions were updated to official Node 24
  releases.
