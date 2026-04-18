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
auth_error_rate = 0
auth_error_type = "invalid_login"

LOGIN_ERRORS = {
    "invalid_login": {
        "errorCode": 403042,
        "errorMessage": "Invalid LoginID",
        "errorDetails": "invalid loginID or password",
    },
    "pending_registration": {
        "errorCode": 206001,
        "errorMessage": "Account Pending Registration",
        "errorDetails": "Missing required fields for registration: preferences.terms.connectlife_terms_conditions.isConsentGranted, preferences.privacy.connectlife_privacy_policy.isConsentGranted",
    },
}

async def login(request):  # noqa: ARG001
    if auth_error_rate > randrange(100):
        return web.Response(
            content_type="application/json",
            text=json.dumps(LOGIN_ERRORS[auth_error_type]),
        )
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

def _gateway_ok(data=None):
    """Return a successful HijuConn gateway response."""
    response = {"resultCode": 0}
    if data is not None:
        response.update(data)
    return web.Response(
        content_type="application/json",
        text=json.dumps({"response": response}),
    )

def _gateway_error(error_code, error_desc):
    """Return an error HijuConn gateway response."""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"response": {
            "resultCode": 1,
            "errorCode": error_code,
            "errorDesc": error_desc,
        }}),
    )

async def get_device_status_list(request):
    if failure_rate > randrange(100):
        return web.Response(status=500)
    if timeout_rate > randrange(100):
        await asyncio.sleep(10.1)
    return _gateway_ok({"deviceList": list(appliances.values())})

async def property_set(request):
    req = await request.json()
    puid = req.get("puid")
    properties = req.get("properties", {})
    if puid not in appliances:
        return _gateway_error(404, f"Unknown puid {puid}")
    appliance = appliances[puid]
    unknowns = [key for key in properties if key not in appliance["statusList"]]
    if unknowns:
        return _gateway_error(400, f"Unknown properties {unknowns}")
    for key in properties:
        appliance["statusList"][key] = properties[key]
    return _gateway_ok()

async def air_duct_energy(request):
    req = await request.json()
    puid = req.get("puid")
    if puid not in appliances:
        return _gateway_error(404, f"Unknown puid {puid}")
    return _gateway_ok({"resultData": {"electricTotal": 0.0, "durationTotal": 0}})

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
    app.add_routes([web.get('/clife-svc/pu/get_device_status_list', get_device_status_list)])
    app.add_routes([web.post('/device/pu/property/set', property_set)])
    app.add_routes([web.post('/clife-svc/pu/air_duct_energy', air_duct_energy)])
    web.run_app(app, port=args.port)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='ConnectLife API test server')
    parser.add_argument('-p', '--port', type=int, default=8080, help='Port on which to serve the web app')
    parser.add_argument('-a', '--auth_error_rate', type=int, default=0, help='Auth error rate in %% for login')
    parser.add_argument('--auth_error_type', choices=list(LOGIN_ERRORS.keys()), default='invalid_login', help='Type of auth error to simulate')
    parser.add_argument('-f', '--failure_rate', type=int, default=0, help='Failure rate in %% for get appliances')
    parser.add_argument('-t', '--timeout_rate', type=int, default=0, help='Timeout rate in %% for get appliances')
    args = parser.parse_args()
    auth_error_rate = args.auth_error_rate
    auth_error_type = args.auth_error_type
    failure_rate = args.failure_rate
    timeout_rate = args.timeout_rate
    main(args)
