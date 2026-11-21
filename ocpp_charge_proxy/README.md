# OCPP Charge Proxy

A Home Assistant app with a virtual OCPP 1.6J chargepoint and an opt-in transparent
relay for a real charger. The companion integration exposes state as HA entities;
the app also has a built-in status page.

- **Emulator** is the default. A separate automation controls any physical load.
- **Relay** forwards unchanged charger/provider messages and records diagnostics.

Provider credentials and a configurable charger endpoint are prerequisites for
relay deployment. Compatibility with a particular provider must be verified.

See [configuration](DOCS.md), [relay setup](RELAY.md) and the
[repository README](https://github.com/abedegno/ocpp_charge_proxy).
