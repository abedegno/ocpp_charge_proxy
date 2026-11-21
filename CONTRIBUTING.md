# Contributing and releases

## Layout

- `ocpp_charge_proxy/`: independently running HA OS app, Python source, app tests,
  Dockerfile, s6 scripts, configuration and user documentation.
- `custom_components/ocpp_charge_proxy/`: companion integration, loaded by Core.
- `tests/`: companion tests using actual Home Assistant classes and build-workflow regressions.
- `.github/workflows/`: reusable tests, app lint and image builds.

Keep the emulator and companion integration when extending relay functionality.
Never connect tests to a live provider or change a user's charger configuration.

## App development

From the repository root, with Python 3.13 or 3.14 and the OpenSSL command installed:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r ocpp_charge_proxy/requirements-dev.txt
python -m pytest ocpp_charge_proxy/tests -q --cov=src --cov-branch --cov-report=term-missing
```

Tests use local loopback sockets and queue-backed OCPP peers. Sandboxed runners
must allow loopback listeners. TLS fixtures generate temporary self-signed test
certificates with OpenSSL. Do not skip these tests to make restricted networking
appear to pass.

To run standalone, change into `ocpp_charge_proxy/`, then use:

```sh
./run_standalone.sh --no-tls localhost:8765 CP001 TEST_PASSWORD
```

The example expects a local mock provider. Use `IO_MODE=relay` and optional
`IO_RELAY_PORT`, `IO_RELAY_CERTFILE`, `IO_RELAY_KEYFILE` for relay testing.
Production upstream WSS remains verified. Credentials supplied as shell arguments
may be visible in process history; use a test account in development.

## Companion tests

Use a separate Python 3.14.2+ environment, matching Core:

```sh
python3.14 -m venv .venv-ha
. .venv-ha/bin/activate
python -m pip install -r requirements-ha-test.txt
python -m pytest tests -q --cov=custom_components.ocpp_charge_proxy --cov-branch
```

The pinned Home Assistant version is the compatibility baseline. Tests exercise
real coordinator/entity/options-flow classes with a local API and an isolated
HTTP session. They do not boot a complete HA OS/Supervisor installation. Verify
discovery, installation, upgrades and both modes in a disposable HA instance
before release. A fake implementation of Home Assistant is not a substitute.

## Coverage and CI

`test.yaml` is reusable and can also be run manually. `builder.yaml` calls it for
pushes and pull requests. App tests run on Python 3.13/3.14, companion tests use
Home Assistant 2026.11.1, and coverage reports are uploaded as artifacts. App
combined line/branch coverage must stay at or above 70%; review missing paths
rather than adding tests that only repeat implementation details.

Image builds depend on the tests succeeding. PR builds use `--test` and do not
publish images. Pushes to main/master can publish to GHCR after tests pass.
`lint.yaml` separately validates app metadata. Do not merge with failing checks.

## Release checklist

1. Work on a feature branch, reproduce reported bugs, add regression coverage and
   update the user documentation and `ocpp_charge_proxy/CHANGELOG.md`.
2. Keep `ocpp_charge_proxy/config.yaml` and the integration's `manifest.json`
   versions in sync. Current prepared release: `0.2.0`.
3. Run both test suites, shell syntax checks, JSON/YAML checks and `git diff --check`.
   Obtain independent review before merging.
4. Build/test the configured aarch64 and amd64 app images. For a local aarch64 build:

   ```sh
   docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.24 \
     --build-arg BUILD_ARCH=aarch64 --build-arg BUILD_VERSION=0.2.0 \
     -t ocpp-charge-proxy:local ocpp_charge_proxy
   ```

5. Verify a disposable HA OS installation and supervised real-provider behavior
   where appropriate. Save the charger endpoint and authentication for rollback.
6. Merge only after CI/review, verify published versioned images for both
   architectures, then create a release with accurate validation and limitations.
   Updating a version string alone does not publish an installable app update.
7. Supervisor offers app updates once the version/image are published. The
   companion in this combined repository is updated manually by replacing its
   files and restarting Core. Marketplace distribution would need an
   integration-only repository.

## Security boundaries

The relay listener requires charger Basic authentication and optionally serves TLS.
Provider TLS verification stays enabled. Do not expose the unauthenticated internal
HTTP API. Diagnostic logs are redacted but still contain device/session metadata.
Do not commit credentials, captures or private keys. A Supervisor security score
is not evidence of a security audit; no fixed score is claimed here.
