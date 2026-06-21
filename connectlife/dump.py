import argparse
import asyncio
import dataclasses
from getpass import getpass
import logging
import json
import sys

from .api import ConnectLifeApi, LifeConnectError
from .trir import TrirConnectLifeApi


def order_dict(dictionary):
    return {k: order_dict(v) if isinstance(v, dict) else v
            for k, v in sorted(dictionary.items())}


def build_api(username: str, password: str, trir: bool):
    if trir:
        return TrirConnectLifeApi(username, password)
    return ConnectLifeApi(username, password)


def feature_code(appliance: dict) -> str | None:
    # TRIR returns featureCode; the default backend returns deviceFeatureCode.
    return appliance.get("deviceFeatureCode") or appliance.get("featureCode")


async def main(
    api: ConnectLifeApi,
    query: str,
    format: str,
    device_type_code: str | None = None,
    device_feature_code: str | None = None,
):
    """Dump the ``query`` data. ``format`` (json/dd) applies only to ``appliances``."""
    if query == "energy":
        await dump_energy(api)
        return
    if query == "static":
        await dump_static(api)
        return
    if query == "property-list":
        if not device_type_code or not device_feature_code:
            raise SystemExit(
                "--query property-list requires --device-type-code and --device-feature-code"
            )
        await dump_property_list(api, device_type_code, device_feature_code)
        return
    appliances = await api.get_appliances_json()
    # Redact private fields
    for appliance in appliances:
        appliance["deviceId"] = "<redacted>"
        appliance["puid"] = "<redacted>"
        appliance["wifiId"] = "<redacted>"
    if format == "json":
        dump_json(appliances)
    if format == "dd":
        dump_data_dictionaries(appliances)


async def dump_energy(api: ConnectLifeApi):
    """Write one JSON file per device with both energy endpoints' responses.

    Probes both ``air_duct_energy`` (air conditioners) and ``energyConsumptionCurve``
    (other appliances) so it's clear which one a device actually reports data on. Uses
    each endpoint's finest period (day / week). The responses carry no private fields.
    """
    seen: dict[str, int] = {}
    for appliance in await api.get_appliances():
        base = f"{appliance.device_type_code}-{appliance.device_feature_code}"
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base}_{seen[base]}"
        air = await api.get_air_duct_energy(
            appliance.puid, appliance.device_type_code, appliance.device_feature_code
        )
        consumption = await api.get_energy_consumption_curve(
            appliance.puid, appliance.device_type_code, appliance.device_feature_code
        )
        result = {
            "deviceTypeCode": appliance.device_type_code,
            "deviceFeatureCode": appliance.device_feature_code,
            "air_duct_energy": dataclasses.asdict(air) if air else None,
            "energy_consumption_curve": dataclasses.asdict(consumption) if consumption else None,
        }
        filename = f"{name}-energy.json"
        with open(filename, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {filename}")


async def dump_static(api: ConnectLifeApi):
    """Write one JSON file per device with its ``query_static_data`` response.

    Keyed by puid, so it can differ between two devices that share a type/feature
    code — useful for working out whether the gateway exposes anything that
    distinguishes otherwise-identical feature codes. For the per-feature-code
    property list, use ``dump_property_list`` (``--format property-list``).

    The response schema is undocumented and may echo identifiers — review and
    redact each file before sharing.
    """
    seen: dict[str, int] = {}
    for appliance in await api.get_appliances():
        base = f"{appliance.device_type_code}-{appliance.device_feature_code}"
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base}_{seen[base]}"
        result = {
            "deviceTypeCode": appliance.device_type_code,
            "deviceFeatureCode": appliance.device_feature_code,
            "query_static_data": await _probe(
                api.query_static_data, appliance.puid
            ),
        }
        filename = f"{name}-static.json"
        with open(filename, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {filename} (review for identifiers before sharing)")


async def dump_property_list(
    api: ConnectLifeApi, device_type_code: str, device_feature_code: str
):
    """Fetch and write the property list for a single type/feature code.

    Unlike the other formats this targets an arbitrary code rather than the
    account's appliances, so it can probe feature codes the account doesn't own.
    """
    result = await _probe(api.get_property_list, device_type_code, device_feature_code)
    filename = f"{device_type_code}-{device_feature_code}-property-list.json"
    with open(filename, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {filename}")


async def _probe(func, *args):
    """Call a probe endpoint, capturing a gateway error instead of aborting.

    A device that doesn't support an endpoint returns a gateway error; recording
    it is as informative as a success, so the dump shows it rather than failing.
    """
    try:
        return await func(*args)
    except LifeConnectError as err:
        return {"error": str(err)}


def dump_json(appliances):
    """Write one JSON file per device, preserving the raw payload."""
    seen: dict[str, int] = {}
    for appliance in appliances:
        base = f'{appliance["deviceTypeCode"]}-{feature_code(appliance)}'
        # Disambiguate multiple devices that share the same type and feature
        # code so they don't overwrite each other (e.g. several identical ACs).
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f'{base}_{seen[base]}'
        filename = f'{name}.json'
        with open(filename, 'w') as f:
            json.dump(order_dict(appliance), f, indent=2)
        print(f'Wrote {filename}')


def dump_data_dictionaries(appliances):
    """Write one data dictionary skeleton per type/feature code.

    Devices that share a type/feature code are merged so the skeleton lists
    every distinct value observed for each property across all of them.
    """
    groups: dict[str, dict] = {}
    for appliance in appliances:
        base = f'{appliance["deviceTypeCode"]}-{feature_code(appliance)}'
        group = groups.setdefault(base, {"nicknames": [], "values": {}})
        group["nicknames"].append(appliance["deviceNickName"])
        for k, v in appliance.get("statusList", {}).items():
            observed = group["values"].setdefault(k, [])
            if v not in observed:
                observed.append(v)
    for base, group in groups.items():
        filename = f'{base}.yaml'
        with open(filename, 'w') as f:
            f.write(f'# {", ".join(group["nicknames"])}\n')
            f.write('properties:\n')
            for k in sorted(group["values"]):
                values = group["values"][k]
                label = "Observed value" if len(values) == 1 else "Observed values"
                f.write(f'- property: {k}\n')
                f.write(f'  # {label}: {", ".join(values)}\n')
        print(f'Wrote {filename}')


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser(
        prog="dump",
        description="Connects to the ConnectLife API and writes a file for each appliance")
    parser.add_argument("-u", "--username")
    parser.add_argument("-p", "--password")
    parser.add_argument(
        "-q",
        "--query",
        choices={
            "appliances": "The account's appliances (default; honors --format)",
            "energy": "Both energy endpoints' responses per device",
            "static": "The per-device query_static_data response per device",
            "property-list": "The property list for --device-type-code/--device-feature-code",
        },
        default="appliances",
        help="What to dump (default: appliances)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices={
            "json": "Dump to JSON file",
            "dd": "Create data dictionary skeleton",
        },
        default="json",
        help="Output format for --query appliances (ignored for other queries)",
    )
    parser.add_argument(
        "--device-type-code",
        help="Device type code (required for --query property-list)",
    )
    parser.add_argument(
        "--device-feature-code",
        help="Device feature code (required for --query property-list)",
    )
    parser.add_argument(
        "-t",
        "--trir",
        action="store_true",
        help="Use the TRIR (Russia/CIS) backend instead of the default",
    )
    parser.add_argument("-v", "--verbose", action='store_true')
    args = parser.parse_args()
    username = args.username if args.username else input("Username: ")
    password = args.password if args.password else getpass()
    if args.verbose:
        logger = logging.getLogger("connectlife")
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    api = build_api(username, password, args.trir)
    asyncio.run(
        main(api, args.query, args.format, args.device_type_code, args.device_feature_code)
    )
