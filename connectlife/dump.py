import argparse
import asyncio
import logging
import json
import sys

from .api import ConnectLifeApi


def order_dict(dictionary):
    return {k: order_dict(v) if isinstance(v, dict) else v
            for k, v in sorted(dictionary.items())}


async def main():
    parser = argparse.ArgumentParser(
        prog="dump",
        description="Connects to the ConnectLife API and prints the response to the '/appliances' request")
    parser.add_argument("username")
    parser.add_argument("password")
    args = parser.parse_args()

    api = ConnectLifeApi(args.username, args.password)
    appliances = await api.get_appliances_json()
    # Redact private fields
    for appliance in appliances:
        appliance["deviceId"] = "<redacted>"
        appliance["puid"] = "<redacted>"
        appliance["wifiId"] = "<redacted>"
    for a in appliances:
        with open(f'{a["deviceTypeCode"]}-{a["deviceFeatureCode"]}.json', 'w') as f:
            json.dump(a, f, indent=2)

if __name__ == "__main__":
    logger = logging.getLogger("connectlife")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    asyncio.run(main())
