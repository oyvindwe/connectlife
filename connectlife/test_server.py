import asyncio
import base64
import datetime as dt
import hashlib
from random import randrange

from aiohttp import web
import argparse
import json
from os import listdir
from os.path import isfile, join

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from connectlife.trir import (
    TRIR_AES_IV,
    TRIR_AES_KEY,
    TRIR_APPS,
    TRIR_TRANSPORT_MAGIC,
    TRIR_VENDOR_SECRET,
)

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
    hours = [str(h) for h in range(24)]
    return _gateway_ok({
        "type": req.get("statType", "day"),
        "dateStart": req.get("dateStart"),
        "dateEnd": req.get("dateEnd"),
        "resultData": {
            "electricTotal": 0.0,
            "costTotal": "0.00",
            "durationTotal": 0,
            "electricCurve": {h: "0.00" for h in hours},
            "costCurve": {h: "0.00" for h in hours},
            "coolingCurve": {h: "0" for h in hours},
            "heatingCurve": {h: "0" for h in hours},
        },
    })

async def energy_consumption_curve(request):
    req = await request.json()
    puid = req.get("puid")
    if puid not in appliances:
        return _gateway_error(404, f"Unknown puid {puid}")
    # Per-day curve across [dateStart, dateEnd] (incl. today) so clients reading
    # curve[today] get a value — the real gateway returns a per-day curve. Sample
    # non-zero values so the daily sensors visibly populate. Year statType uses
    # YYYY-MM and is left empty.
    electric_curve: dict[str, str] = {}
    water_curve: dict[str, str] = {}
    try:
        day = dt.date.fromisoformat(req["dateStart"])
        end = dt.date.fromisoformat(req["dateEnd"])
        while day <= end:
            electric_curve[day.isoformat()] = "1.0"
            water_curve[day.isoformat()] = "11.0"
            day += dt.timedelta(days=1)
    except (KeyError, ValueError):
        pass
    return _gateway_ok({
        "type": req.get("statType", "week"),
        "deviceType": req.get("deviceType"),
        "resultData": {
            "electricUsage": f"{len(electric_curve):.2f}",
            "waterUsage": f"{len(water_curve) * 11:.2f}",
            "normElectricUsage": "0.00",
            "normWaterUsage": "0.00",
            "runTimes": "0.00",
            "cycles": len(electric_curve),
            "electricCurve": electric_curve,
            "waterCurve": water_curve,
            "programResult": [],
            "energyPeriod": [],
        },
    })

async def query_static_data(request):
    req = await request.json()
    puid = req.get("puid")
    if puid not in appliances:
        return _gateway_error(404, f"Unknown puid {puid}")
    # Stub: echo the device's feature code so per-puid responses differ between
    # devices the way the real gateway's capability data is expected to.
    appliance = appliances[puid]
    return _gateway_ok({"data": {"deviceFeatureCode": appliance.get("deviceFeatureCode")}})

async def get_property_list(request):  # noqa: ARG001
    return _gateway_ok({"properties": []})

# -- TRIR (Russia/CIS) backend ---------------------------------------------

# Canned tokens returned by the TRIR login/refresh endpoints. The *ExpiredTime
# fields are lifetimes in seconds, matching the real gateway.
TRIR_TOKENS = {
    "customerId": "12345678901234567",
    "accessToken": "trir_access_token",
    "accessTokenCreateTime": 0,
    "accessTokenExpiredTime": 86400,
    "refreshToken": "trir_refresh_token",
    "refreshTokenExpiredTime": 2592000,
}


def _trir_ok(data=None):
    """Return a successful TRIR gateway response envelope."""
    response = {"resultCode": 0, "errorCode": 0, "errorDesc": None}
    if data is not None:
        response.update(data)
    return web.Response(
        content_type="application/json",
        text=json.dumps({"response": response, "signatureServer": "test"}),
    )


def _trir_error(error_code, error_desc):
    """Return a TRIR error response envelope."""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"response": {
            "resultCode": 1,
            "errorCode": error_code,
            "errorDesc": error_desc,
        }, "signatureServer": "test"}),
    )


def _trir_decode_body(body: str) -> dict:
    """Decode a TRIR request body (HENC-wrapped or plain JSON) to a dict."""
    if body.startswith(TRIR_TRANSPORT_MAGIC):
        ciphertext = base64.b64decode(body[len(TRIR_TRANSPORT_MAGIC):])
        decryptor = Cipher(algorithms.AES(TRIR_AES_KEY), modes.CBC(TRIR_AES_IV)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        body = (unpadder.update(padded) + unpadder.finalize()).decode()
    return json.loads(body)


def _trir_app_secret(app_id):
    for app in TRIR_APPS.values():
        if app["app_id"] == app_id:
            return app["app_secret"]
    return None


def _trir_expected_sign(data: dict, app_secret: str) -> str:
    items = []
    for key in sorted(k for k in data if k != "sign"):
        value = data[key]
        if not value:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"))
        items.append(f"{key}={value}")
    sign_str = f"{app_secret}{'&'.join(items)}{TRIR_VENDOR_SECRET}"
    return hashlib.sha1(sign_str.encode()).hexdigest()


def _trir_verify(data: dict):
    """Return an error Response if the request is malformed, else None."""
    app_secret = _trir_app_secret(data.get("appId"))
    if app_secret is None:
        return _trir_error(600902, f"Unknown appId {data.get('appId')}")
    if data.get("sign") != _trir_expected_sign(data, app_secret):
        return _trir_error(600903, "Signature mismatch")
    return None


async def trir_login(request):
    try:
        data = _trir_decode_body(await request.text())
    except Exception as err:  # noqa: BLE001
        return _trir_error(600901, f"Unable to decode request body: {err}")
    if auth_error_rate > randrange(100):
        return _trir_error(600904, "User name or password error")
    if (error := _trir_verify(data)) is not None:
        return error
    return _trir_ok(TRIR_TOKENS)


async def trir_refresh_token(request):
    data = _trir_decode_body(await request.text())
    if (error := _trir_verify(data)) is not None:
        return error
    return _trir_ok(TRIR_TOKENS)


async def trir_device_tab_list(request):  # noqa: ARG001
    if failure_rate > randrange(100):
        return web.Response(status=500)
    if timeout_rate > randrange(100):
        await asyncio.sleep(10.1)
    return _trir_ok({
        "deviceList": list(appliances.values()),
        "tabCardList": [],
        "roomList": [],
        "floorList": [],
        "locationList": None,
        "ext": {},
    })


def main(args):
    directory = args.directory
    filenames = list(filter(lambda f: f[-5:] == ".json", [f for f in listdir(directory) if isfile(join(directory, f))]))
    for filename in filenames:
        with (open(join(directory, filename)) as f):
            appliance = json.load(f)
            appliance["deviceId"] = filename[0:-5]
            appliance["puid"] = f"puid{appliance['deviceId']}"
            # Expose both feature code fields so a single running server can
            # serve EU (deviceFeatureCode) and TRIR (featureCode) clients,
            # regardless of which dump format the base file uses.
            feature = appliance.get("deviceFeatureCode") or appliance.get("featureCode")
            appliance["deviceFeatureCode"] = feature
            appliance["featureCode"] = feature
            appliance["deviceNickName"] = f'{appliance["deviceNickName"]} ({appliance["deviceTypeCode"]}-{feature})'
            appliances[appliance["puid"]] = appliance

    app = web.Application()
    app.add_routes([web.post('/accounts.login', login)])
    app.add_routes([web.post('/accounts.getJWT', get_jwt)])
    app.add_routes([web.post('/oauth/authorize', authorize)])
    app.add_routes([web.post('/oauth/token', token)])
    app.add_routes([web.get('/clife-svc/pu/get_device_status_list', get_device_status_list)])
    app.add_routes([web.post('/device/pu/property/set', property_set)])
    app.add_routes([web.post('/clife-svc/pu/air_duct_energy', air_duct_energy)])
    app.add_routes([web.post('/clife-svc/pu/energyConsumptionCurve', energy_consumption_curve)])
    app.add_routes([web.post('/clife-svc/pu/query_static_data', query_static_data)])
    app.add_routes([web.get('/clife-svc/get_property_list', get_property_list)])
    # TRIR (Russia/CIS) backend. property/set and air_duct_energy are shared.
    app.add_routes([web.post('/account/acc/login_pwd', trir_login)])
    app.add_routes([web.post('/account/acc/refresh_token', trir_refresh_token)])
    app.add_routes([web.post('/br/getDeviceTabList', trir_device_tab_list)])
    web.run_app(app, port=args.port)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='ConnectLife API test server')
    parser.add_argument('-p', '--port', type=int, default=8080, help='Port on which to serve the web app')
    parser.add_argument('-d', '--directory', default='.', help='Directory to read dump files from')
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
