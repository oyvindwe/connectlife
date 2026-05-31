import argparse
import asyncio
import dataclasses
from getpass import getpass
import logging
import json
import sys

from .api import ConnectLifeApi
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


async def main(api: ConnectLifeApi, format: str):
    if format == "energy":
        await dump_energy(api)
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
        "-f",
        "--format",
        choices={
            "json": "Dump to JSON file",
            "dd": "Create data dictionary skeleton",
            "energy": "Dump both energy endpoints' responses per device",
        },
        default="json"
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
    asyncio.run(main(api, args.format))
