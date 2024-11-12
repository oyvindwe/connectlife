import datetime as dt
import json
from typing import Any, Sequence

import aiohttp
import logging

from .appliance import ConnectLifeAppliance

_LOGGER = logging.getLogger(__name__)


class LifeConnectError(Exception):
    pass


class LifeConnectAuthError(Exception):
    pass


class ConnectLifeApi:
    api_key = "4_yhTWQmHFpZkQZDSV1uV-_A"
    client_id = "5065059336212"
    client_secret = "07swfKgvJhC3ydOUS9YV_SwVz0i4LKqlOLGNUukYHVMsJRF1b-iWeUGcNlXyYCeK"

    login_url = "https://accounts.eu1.gigya.com/accounts.login"
    jwt_url = "https://accounts.eu1.gigya.com/accounts.getJWT"

    oauth2_redirect = "https://api.connectlife.io/swagger/oauth2-redirect.html"
    oauth2_authorize = "https://oauth.hijuconn.com/oauth/authorize"
    oauth2_token = "https://oauth.hijuconn.com/oauth/token"

    appliances_url = "https://connectlife.bapi.ovh/appliances"

    def __init__(self, username: str, password: str, test_server: str = None):
        """Initialize the auth."""
        if test_server:
            self.login_url = f"{test_server}/accounts.login"
            self.jwt_url = f"{test_server}/accounts.getJWT"
            self.oauth2_redirect = f"{test_server}/swagger/oauth2-redirect.html"
            self.oauth2_authorize = f"{test_server}/oauth/authorize"
            self.oauth2_token = f"{test_server}/oauth/token"
            self.appliances_url = f"{test_server}/appliances"

        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._expires: dt.datetime | None = None
        self._refresh_token: str | None = None
        self.appliances: Sequence[ConnectLifeAppliance] = []

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        async with aiohttp.ClientSession() as session:
            async with session.post(self.login_url, data={
                "loginID": self._username,
                "password": self._password,
                "APIKey": self.api_key,
            }) as response:
                if response.status == 200:
                    body = await self._json(response)
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
            async with session.get(self.appliances_url, headers={
                "User-Agent": "connectlife-api-connector 2.1.4",
                "X-Token": self._access_token
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectError(f"Unexpected response: status={response.status}")
                return await response.json()

    async def update_appliance(self, puid: str, properties: dict[str, str]):
        data = {
            "puid": puid,
            "properties": properties
        }
        _LOGGER.debug("Updating appliance with puid %s to %s", puid, json.dumps(properties))
        await self._fetch_access_token()
        async with aiohttp.ClientSession() as session:
            async with session.post(self.appliances_url, json=data, headers={
                "User-Agent": "connectlife-api-connector 2.1.4",
                "X-Token": self._access_token
            }) as response:
                result = await response.text()
                _LOGGER.debug(result)
        _LOGGER.debug("Updated appliance with puid %s", puid)

    async def _fetch_access_token(self):
        if self._expires is None:
            await self._initial_access_token()
        elif self._expires < dt.datetime.now():
            await self._refresh_access_token()

    async def _initial_access_token(self):
        async with aiohttp.ClientSession() as session:
            async with session.post(self.login_url, data={
                "loginID": self._username,
                "password": self._password,
                "APIKey": self.api_key
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from login: status={response.status}")
                body = await self._json(response)
                error_code = body["errorCode"] if "errorCode" in body else None
                error_message = body["errorMessage"] if "errorMessage" in body else None
                error_details = body["errorDetails"] if "errorDetails" in body else None
                if error_code or error_message or error_details:
                    raise LifeConnectAuthError(f"Failed to login. Code: {error_code} Message: '{error_message}' Details: '{error_details}'")
                uid = self._require_auth_field(body, "UID")
                session_info = self._require_auth_field(body, "sessionInfo")
                if "cookieValue" not in session_info:
                    _LOGGER.info(f"Missing 'sessionInfo.cookieValue' in response: {response}")
                    raise LifeConnectAuthError(f"Missing 'sessionInfo.cookieValue' in response")
                login_token = body["sessionInfo"]["cookieValue"]

            async with session.post(self.jwt_url, data={
                "APIKey": self.api_key,
                "login_token":  login_token
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from getJWT: status={response.status}")
                body = await self._json(response)
                if "id_token" not in body:
                    raise LifeConnectAuthError(f"Missing 'id_token' in response")
                id_token = body["id_token"]

            async with session.post(self.oauth2_authorize, json={
                "client_id": self.client_id,
                "redirect_uri": self.oauth2_redirect,
                "idToken":  id_token,
                "response_type": "code",
                "thirdType": "CDC",
                "thirdClientId": uid,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from authorize: status={response.status}")
                body = await response.json()
                code = self._require_auth_field(body, "code")

            async with session.post(self.oauth2_token, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.oauth2_redirect,
                "grant_type": "authorization_code",
                "code": code,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from initial access token: status={response.status}")
                body = await self._json(response)
                self._access_token = self._require_auth_field(body, "access_token")
                expires_in = self._require_auth_field(body, "expires_in")
                # Renew 90 seconds before expiration
                self._expires = dt.datetime.now() + dt.timedelta(0, expires_in - 90)
                self._refresh_token = self._require_auth_field(body, "refresh_token")

    async def _refresh_access_token(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(self.oauth2_token, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.oauth2_redirect,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }) as response:
                if response.status != 200:
                    _LOGGER.debug(f"Response status code: {response.status}")
                    _LOGGER.debug(response.headers)
                    _LOGGER.debug(await response.text())
                    raise LifeConnectAuthError(f"Unexpected response from refreshing access token: status={response.status}")
                body = await response.json()
                self._access_token = self._require_auth_field(body, "access_token")
                expires_in = self._require_auth_field(body, "expires_in")
                # Renew 90 seconds before expiration
                self._expires = dt.datetime.now() + dt.timedelta(0, expires_in - 90)

    @staticmethod
    async def _json(response: aiohttp.ClientResponse) -> Any:
        # response may have wrong content-type, cannot use response.json()
        text = await response.text()
        _LOGGER.debug(f"response: {text}")
        return json.loads(text)

    @staticmethod
    def _require_auth_field(response: dict[str, Any], field: str):
        if field not in response:
            _LOGGER.info(f"Missing '{field}' in response: {response}")
            raise LifeConnectAuthError(f"Missing '{field}' in response")
        return response[field]
