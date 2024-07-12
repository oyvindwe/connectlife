from aiohttp import web
import json
from os import listdir
from os.path import isfile, join


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
                text='{"resultCode":0,"kvMap":null,"errorCode":0,"errorDesc":null}'
            )
        unknowns = [key for key in req["properties"] if key not in appliance["statusList"]]
        return web.Response(
            content_type="application/json",
            text=f'{"resultCode":-1,"kvMap":null,"errorCode":400,"errorDesc":"Unknown properies {unknowns}"}'
        )
    else:
        return web.Response(
            content_type="application/json",
            text=f'{"resultCode":-1,"kvMap":null,"errorCode":404,"errorDesc":"Unknown puid {req["puid"]}"}'
        )



filenames = list(filter(lambda f: f[-5:] == ".json", [f for f in listdir(".") if isfile(join(".", f))]))
appliances = {}
for filename in filenames:
    with (open(filename) as f):
        appliance = json.load(f)
        appliance["deviceId"] = filename[0:-5]
        appliance["puid"] = f"puid{appliance['deviceId']}"
        appliances[appliance["puid"]] = appliance

app = web.Application()
app.add_routes([web.post('/accounts.login', login)])
app.add_routes([web.post('/accounts.getJWT', get_jwt)])
app.add_routes([web.post('/oauth/authorize', authorize)])
app.add_routes([web.post('/oauth/token', token)])
app.add_routes([web.get('/appliances', get_appliances)])
app.add_routes([web.post('/appliances', update_appliance)])
web.run_app(app)
