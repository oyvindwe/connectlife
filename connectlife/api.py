import datetime as dt
import json
from typing import Any, Sequence

import aiohttp
import logging

from .appliance import ConnectLifeAppliance

API_KEY = "4_yhTWQmHFpZkQZDSV1uV-_A"
CLIENT_ID = "5065059336212"
CLIENT_SECRET = "07swfKgvJhC3ydOUS9YV_SwVz0i4LKqlOLGNUukYHVMsJRF1b-iWeUGcNlXyYCeK"

LOGIN_URL = "https://accounts.eu1.gigya.com/accounts.login"
JWT_URL = "https://accounts.eu1.gigya.com/accounts.getJWT"

OAUTH2_REDIRECT = "https://api.connectlife.io/swagger/oauth2-redirect.html"
OAUTH2_AUTHORIZE = "https://oauth.hijuconn.com/oauth/authorize"
OAUTH2_TOKEN = "https://oauth.hijuconn.com/oauth/token"

APPLIANCES_URL = "https://connectlife.bapi.ovh/appliances"

_LOGGER = logging.getLogger(__name__)


class LifeConnectError(Exception):
    pass


class LifeConnectAuthError(Exception):
    pass


class ConnectLifeApi():
    def __init__(self, username: str, password: str):
        """Initialize the auth."""
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._expires: dt.datetime | None = None
        self._refresh_token: str | None = None
        self.appliances: Sequence[ConnectLifeAppliance] = []


    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        async with aiohttp.ClientSession() as session:
            async with session.post(LOGIN_URL, data={
                "loginID": self._username,
                "password": self._password,
                "APIKey": API_KEY,
            }) as response:
                if response.status == 200:
                    body = await self._json(response)
                    if "UID" in body:
                        self.uid = body["UID"]
                    return "UID" in body and "sessionInfo" in body and "cookieValue" in body["sessionInfo"]
        return False

    async def login(self) -> None:
        await self._fetch_access_token()


    async def get_appliances(self) -> Any:
        """Make a request."""
        appliances = await self.get_appliances_json()
        self.appliances = [ConnectLifeAppliance(self, a) for a in appliances if "deviceId" in a]
        return self.appliances


    async def get_appliances_json(self) -> Any:
        """Make a request and return the response as text."""
        await self._fetch_access_token()
        async with aiohttp.ClientSession() as session:
            async with session.get(APPLIANCES_URL, headers={
                "User-Agent": "connectlife-api-connector 2.1.4",
                "X-Token": self._access_token
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectError(f"Unexpected response: status={response.status}")
                return await response.json()


    async def _fetch_access_token(self):
        if self._expires is None:
            await self._initial_access_token()
        elif self._expires < dt.datetime.now():
            await self._refresh_access_token()


    async def _initial_access_token(self):
        async with aiohttp.ClientSession() as session:
            async with session.post(LOGIN_URL, data={
                "loginID": self._username,
                "password": self._password,
                "APIKey": API_KEY,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from login: status={response.status}")
                body = await self._json(response)
                uid = body["UID"]
                login_token = body["sessionInfo"]["cookieValue"]

            async with session.post(JWT_URL, data={
                "APIKey": API_KEY,
                "login_token":  login_token
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from getJWT: status={response.status}")
                body = await self._json(response)
                id_token = body["id_token"]

            async with session.post(OAUTH2_AUTHORIZE, json={
                "client_id": CLIENT_ID,
                "redirect_uri": OAUTH2_REDIRECT,
                "idToken":  id_token,
                "response_type": "code",
                "thirdType":"CDC",
                "thirdClientId": uid,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from authorize: status={response.status}")
                body = await response.json()
                code = body["code"]

            async with session.post(OAUTH2_TOKEN, data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": OAUTH2_REDIRECT,
                "grant_type": "authorization_code",
                "code": code,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from initial access token: status={response.status}")
                body = await self._json(response)
                self._access_token = body["access_token"]
                # Renew 90 seconds before expiration
                self._expires = dt.datetime.now() + dt.timedelta(0, body["expires_in"] - 90)
                self._refresh_token = body["refresh_token"]


    async def _refresh_access_token(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(OAUTH2_TOKEN, data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": OAUTH2_REDIRECT,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from refreshing access token: status={response.status}")
                body = await response.json()
                self._access_token = body["access_token"]
                # Renew 90 seconds before expiration
                self._expires = dt.datetime.now() + dt.timedelta(0, body["expires_in"] - 90)


    async def _json(self, response: aiohttp.ClientResponse) -> Any:
        # response may have wrong content-type, cannot use response.json()
        text = await response.text()
        _LOGGER.debug(f"response: {text}")
        return json.loads(text)
