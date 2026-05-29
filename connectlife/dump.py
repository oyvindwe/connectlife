import argparse
import asyncio
from getpass import getpass
import logging
import json
import sys

from .api import ConnectLifeApi
from .trir import TrirConnectLifeApi


def order_dict(dictionary):
    return {k: order_dict(v) if isinstance(v, dict) else v
            for k, v in sorted(dictionary.items())}


def build_api(username: str, password: str, trir: bool, device_uuid: str | None, platform: str):
    if trir:
        return TrirConnectLifeApi(
            username, password, device_uuid=device_uuid, platform=platform
        )
    return ConnectLifeApi(username, password)


async def main(api: ConnectLifeApi, format: str):
    appliances = await api.get_appliances_json()
    # Redact private fields
    for appliance in appliances:
        appliance["deviceId"] = "<redacted>"
        appliance["puid"] = "<redacted>"
        appliance["wifiId"] = "<redacted>"
        if format == "json":
            with open(f'{appliance["deviceTypeCode"]}-{appliance["deviceFeatureCode"]}.json', 'w') as f:
                json.dump(order_dict(appliance), f, indent=2)
        if format == "dd":
            status_list = appliance.get("statusList", {})
            with open(f'{appliance["deviceTypeCode"]}-{appliance["deviceFeatureCode"]}.yaml', 'w') as f:
                f.write(f'# {appliance["deviceNickName"]}\n')
                f.write('properties:\n')
                for k, v in sorted(status_list.items()):
                    f.write(f'- property: {k}\n')
                    f.write(f'  # Sample value: {v}\n')


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
            "dd": "Create data dictionary skeleton"
        },
        default="json"
    )
    parser.add_argument(
        "-t",
        "--trir",
        action="store_true",
        help="Use the TRIR (Russia/CIS) backend instead of the default",
    )
    parser.add_argument(
        "--device-uuid",
        help="TRIR only: stable device UUID (generated if omitted)",
    )
    parser.add_argument(
        "--platform",
        choices=["android", "ios"],
        default="android",
        help="TRIR only: app platform to emulate (default: android)",
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

    api = build_api(username, password, args.trir, args.device_uuid, args.platform)
    asyncio.run(main(api, args.format))
