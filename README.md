# Python library for ConnectLife API 

Used by devices from Hisense, Gorenje, ASKO & ATAG and ETNA Connect.

The goal of this library is to support a native [Home Assistant](https://www.home-assistant.io/) integration for devices
that uses the ConnectLife API.

The code is based on [Connectlife API proxy / MQTT Home Assistant integration](https://github.com/bilan/connectlife-api-connector)
([MIT license](https://github.com/bilan/connectlife-api-connector/blob/51c6b8e4562205e1c343d0cba19354f411bd5e77/composer.json#L2-L6)).

Software is provided as is - use at your own risk. There is probably no way to harm your physical devices, but
there is no guarantee that you don't experience other problems, for instance locking your ConnectLife account. 

Licensed under [GPLv3](LICENSE).

To test out the library (users in Russia/CIS may need to pass the `--trir` option):
```bash
pip install connectlife
python -m connectlife.dump --username <username> [--password '<password>'] [--trir]
```
Omit password to be prompted for it instead. Make sure to always use single quotes around
passwords with special characters.

This will log in to the ConnectLife API using the provided username and password, and write a JSON
file with all returned fields for each appliance that is registered with the account. Pass
`--format dd` to instead write a mapping file YAML skeleton to be used with
[connectlife-ha](https://github.com/oyvindwe/connectlife-ha).

> **Note:** the property set a device reports is not authoritative. A device may report properties it
> does not actually support (and may omit ones its feature code defines), so two units with the same
> device type/feature code can report different properties. Treat dumps as a starting point for a
> mapping, not ground truth — cross-check controls against the device or the ConnectLife app.

To instead dump each appliance's energy statistics (both the `air_duct_energy` and
`energyConsumptionCurve` endpoints), use `--query energy`:
```bash
python -m connectlife.dump --username <username> --query energy
```

To dump each appliance's per-device static data (the `query_static_data` endpoint, keyed by puid),
use `--query static`:
```bash
python -m connectlife.dump --username <username> --query static
```
Because it's keyed by the device's puid, the response can differ between two physical models that
report the same device type/feature code. The response echoes the puid, so the dump redacts the
puid, wifi_id and device_id — but the schema is gateway-defined and may contain other identifiers,
so review each file before sharing. (For the per-feature-code property list, use
`--query property-list` below.)

Two more per-device queries work the same way (one file per device, known identifiers redacted):
- `--query system` dumps `query_system_data`. For a dishwasher it lists the programs with their
  position, duration and which options each allows.
- `--query self-check` dumps the device's self-check: the faults it can report (e.g. `Error-F11`,
  with the value that means a fault and a description) and the current ones.

The library also has `get_hour_power(puid, date)` for the hourly energy use of a day. Non-AC devices
reply with zeros.

To fetch the property list for a specific device type and feature code (any code, not just ones on
your account), use `--query property-list`:
```bash
python -m connectlife.dump --username <username> \
    --query property-list --device-type-code <type-code> --device-feature-code <feature-code>
```

## OAuth (browser sign-in)

`OAuthConnectLifeApi` uses tokens from the browser sign-in on `oauth.hijuconn.com`, which also
supports Google, Apple, Facebook and Microsoft accounts and accepting updated terms. The caller
runs the authorization code flow and refreshes the tokens (in Home Assistant, the OAuth2 config
flow), and passes a callback returning a valid access token:

```python
from connectlife.oauth import (
    OAUTH2_AUTHORIZE_URL,
    OAUTH2_CLIENT_ID,
    OAUTH2_CLIENT_SECRET,
    OAUTH2_TOKEN_URL,
    OAuthConnectLifeApi,
    generate_source_id,
)

api = OAuthConnectLifeApi(get_access_token, source_id=stored_source_id)
appliances = await api.get_appliances()
```

The client ID and secret are Hisense's client for Home Assistant. Requests go to Hisense's
third-party API (`juapi-3rd.hijuconn.com`), except energy statistics, which it doesn't have; those
go to the app gateway with the same token. If the API rejects the token twice,
`LifeConnectAuthError` is raised and the sign-in should be repeated. Persist the `source_id`.

The Home Assistant integration is currently in discovery phase. Please contribute your device dumps to help
the development.

## Test server

To use the test server to support developing the Home Assistant integration, clone this repo and see
[DEVELOPMENT.md](DEVELOPMENT.md#test-server):
