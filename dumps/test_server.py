import asyncio
from random import randrange

from aiohttp import web
import argparse
import json
from os import listdir
from os.path import isfile, join

appliances = {}
failure_rate = 0
timeout_rate = 0

async def login(request):
    return web.Response(
        content_type="application/json",
        text='{"UID": "123", "sessionInfo":{"cookieValue": "my_login_token"}}'
    )

async def get_jwt(request):
    return web.Response(
        content_type="application/json",
        text='{"id_token": "my_id_token"}'
    )

async def authorize(request):
    return web.Response(
        content_type="application/json",
        text='{"code": "my_authorization_token"}'
    )

async def token(request):
    return web.Response(
        content_type="application/json",
        text='{"access_token": "my_access_token", "expires_in": 86400, "refresh_token": "my_refresh_token"}'
    )

async def get_appliances(request):
    if failure_rate > randrange(100):
        return web.Response(status=500)
    if timeout_rate > randrange(100):
        await asyncio.sleep(10.1)
    return web.Response(
        content_type="application/json",
        text=json.dumps(list(appliances.values()))
    )

async def update_appliance(request):
    req = await request.json()
    if req["puid"] in appliances:
        appliance = appliances[req["puid"]]
        if all(k in appliance["statusList"] for k in req["properties"]):
            for key in req["properties"]:
                appliance["statusList"][key] = req["properties"][key]
            return web.Response(
                content_type="application/json",
                text=json.dumps({"resultCode": 0, "kvMap": None, "errorCode":0, "errorDesc": None})
            )
        unknowns = [key for key in req["properties"] if key not in appliance["statusList"]]
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "resultCode": -1,
                "kvMap": None,
                "errorCode": 400,
                "errorDesc": f"Unknown properties {unknowns}"
            })
        )
    else:
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "resultCode": -1,
                "kvMap": None,
                "errorCode": 404,
                "errorDesc": f'Unknown puid {req["puid"]}'
            })
        )
def main(args):
    filenames = list(filter(lambda f: f[-5:] == ".json", [f for f in listdir(".") if isfile(join(".", f))]))
    for filename in filenames:
        with (open(filename) as f):
            appliance = json.load(f)
            appliance["deviceId"] = filename[0:-5]
            appliance["puid"] = f"puid{appliance['deviceId']}"
            appliance["deviceNickName"] = f'{appliance["deviceNickName"]} ({appliance["deviceTypeCode"]}-{appliance["deviceFeatureCode"]})'
            appliances[appliance["puid"]] = appliance

    app = web.Application()
    app.add_routes([web.post('/accounts.login', login)])
    app.add_routes([web.post('/accounts.getJWT', get_jwt)])
    app.add_routes([web.post('/oauth/authorize', authorize)])
    app.add_routes([web.post('/oauth/token', token)])
    app.add_routes([web.get('/appliances', get_appliances)])
    app.add_routes([web.post('/appliances', update_appliance)])
    web.run_app(app, port=args.port)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='ConnectLife API test server')
    parser.add_argument('-p', '--port', type=int, default=8080, help='Port on which to serve the web app')
    parser.add_argument('-f', '--failure_rate', type=int, default=0, help='Failure rate in % for get appliances')
    parser.add_argument('-t', '--timeout_rate', type=int, default=0, help='Timeout rate in % for get appliances')
    args = parser.parse_args()
    failure_rate = args.failure_rate
    timeout_rate = args.timeout_rate
    main(args)
