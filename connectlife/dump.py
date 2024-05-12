import argparse

import asyncio
import json

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
    print(json.dumps([order_dict(a) for a in appliances], indent=2))

if __name__ == "__main__":
    asyncio.run(main())

