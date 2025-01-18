import argparse
import asyncio
from getpass import getpass
import logging
import json
import sys

from .api import ConnectLifeApi


def order_dict(dictionary):
    return {k: order_dict(v) if isinstance(v, dict) else v
            for k, v in sorted(dictionary.items())}


async def main(username: str, password: str, format: str):
    api = ConnectLifeApi(username, password)
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
            with open(f'{appliance["deviceTypeCode"]}-{appliance["deviceFeatureCode"]}.yaml', 'w') as f:
                f.write(f'# {appliance["deviceNickName"]}\n')
                f.write('properties:\n')
                for k, v in sorted(appliance["statusList"].items()):
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

    asyncio.run(main(username, password, args.format))
