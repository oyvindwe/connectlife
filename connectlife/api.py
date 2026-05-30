"""ConnectLife API client using the HijuConn gateway."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
from typing import Any, Sequence, cast

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .appliance import ConnectLifeAppliance

_LOGGER = logging.getLogger(__name__)

AUTH_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
GATEWAY_RANDSTR_CHECK_FAILED = 101005

DEFAULT_OAUTH_REDIRECT_URI = "https://api.connectlife.io/swagger/oauth2-redirect.html"

try:
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("connectlife")
except Exception:
    _VERSION = "dev"
GATEWAY_USER_AGENT = f"connectlife/{_VERSION}"
GATEWAY_BASE_URL = "https://clife-eu-gateway.hijuconn.com"
GATEWAY_DEVICE_LIST_URL = f"{GATEWAY_BASE_URL}/clife-svc/pu/get_device_status_list"
GATEWAY_UPDATE_URL = f"{GATEWAY_BASE_URL}/device/pu/property/set"
GATEWAY_ENERGY_URL = f"{GATEWAY_BASE_URL}/clife-svc/pu/air_duct_energy"
GATEWAY_APP_ID = "47110565134383"
GATEWAY_APP_SECRET = "yOzhz6junYno-nmULM3Wr7PU_dpSZN22ZdluvVWZ4uW5ZwwG8fIGCHTbrhcnU-iv"
GATEWAY_LANGUAGE_ID = "12"
GATEWAY_TIMEZONE = "1.0"
GATEWAY_VERSION = "5.0"
GATEWAY_SIGN_SUFFIX = "D9519A4B756946F081B7BB5B5E8D1197"
GATEWAY_INVALID_ACCESS_TOKEN = 100026
GATEWAY_PUBLIC_KEY = cast(
    RSAPublicKey,
    serialization.load_pem_public_key(
        b"-----BEGIN PUBLIC KEY-----\n"
        b"MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAyyWrNG6q475HIHu7sMVu\n"
        b"vHof6vlgPeixmxa4EL/UsvVvHPz33NnWoQetQqit9TBNzUjMXw0KlY9PXM4iqHUU\n"
        b"U+dSyNDq1jZWIiJ2C2FccppswJtIKL3NRMFvT9PFh6NlP/4FUcQKojgKFbF7Kacc\n"
        b"JPKYHlwaO7qgoIjLxAHlSOXGpucJcOkPzT2EqsSVnW8sn8kenvNmghXDayhgxsh6\n"
        b"AyxK4kehJplEnmX/iYCfNoFXknGcLqFWYccgBz3fybvx30C/0IgU1980L8QsUAv5\n"
        b"esZmN8ugnbRgLRxKRlkQQLxQAiZMZdKTAx665YflT3YMHJvEFE8c2XFgoxHzSMc4\n"
        b"BwIDAQAB\n"
        b"-----END PUBLIC KEY-----\n"
    ),
)


class LifeConnectError(Exception):
    """Base ConnectLife API error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


class LifeConnectAuthError(LifeConnectError):
    """Authentication failure against ConnectLife."""


class ConnectLifeApi:
    """ConnectLife API client."""

    api_key = "4_yhTWQmHFpZkQZDSV1uV-_A"

    login_url = "https://accounts.eu1.gigya.com/accounts.login"
    jwt_url = "https://accounts.eu1.gigya.com/accounts.getJWT"

    oauth2_authorize = "https://oauth.hijuconn.com/oauth/authorize"
    oauth2_token = "https://oauth.hijuconn.com/oauth/token"

    client_id = "5065059336212"
    client_secret = "07swfKgvJhC3ydOUS9YV_SwVz0i4LKqlOLGNUukYHVMsJRF1b-iWeUGcNlXyYCeK"
    oauth2_redirect_uri = DEFAULT_OAUTH_REDIRECT_URI

    request_timeout = aiohttp.ClientTimeout(total=30)

    gateway_device_list_url = GATEWAY_DEVICE_LIST_URL
    gateway_update_url = GATEWAY_UPDATE_URL
    gateway_energy_url = GATEWAY_ENERGY_URL

    # Overridable so alternative backends (e.g. TRIR) can vary the client
    # identity and the gateway error codes they react to.
    gateway_user_agent = GATEWAY_USER_AGENT
    invalid_access_token_code = GATEWAY_INVALID_ACCESS_TOKEN
    randstr_check_failed_code = GATEWAY_RANDSTR_CHECK_FAILED

    def __init__(
        self,
        username: str,
        password: str,
        test_server: str | None = None,
    ):
        """Initialize the client."""
        if test_server:
            self.login_url = f"{test_server}/accounts.login"
            self.jwt_url = f"{test_server}/accounts.getJWT"
            self.oauth2_authorize = f"{test_server}/oauth/authorize"
            self.oauth2_token = f"{test_server}/oauth/token"
            self.oauth2_redirect_uri = f"{test_server}/swagger/oauth2-redirect.html"
            self.gateway_device_list_url = f"{test_server}/clife-svc/pu/get_device_status_list"
            self.gateway_update_url = f"{test_server}/device/pu/property/set"
            self.gateway_energy_url = f"{test_server}/clife-svc/pu/air_duct_energy"

        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._expires: dt.datetime | None = None
        self._refresh_token: str | None = None
        self._refresh_token_expires: dt.datetime | None = None
        self.appliances: Sequence[ConnectLifeAppliance] = []

    async def authenticate(self) -> bool:
        """Test whether the full ConnectLife login flow succeeds."""
        try:
            await self.login()
        except LifeConnectError:
            return False
        return True

    async def login(self) -> None:
        """Force a fresh login, resetting all tokens first."""
        self._reset_tokens()
        await self._fetch_access_token()

    async def get_appliances(self) -> Sequence[ConnectLifeAppliance]:
        """Fetch appliances and update the cached appliance list."""
        appliances = self._normalize_appliance_payloads(await self.get_appliances_json())
        self.appliances = [ConnectLifeAppliance(self, a) for a in appliances if "deviceId" in a]
        return self.appliances

    async def get_appliances_json(self) -> Any:
        """Fetch the appliance list as JSON via the HijuConn gateway."""
        await self._fetch_access_token()
        return await self._request_gateway_appliances_json(retry_on_reauth=True)

    async def update_appliance(self, puid: str, properties: dict[str, str]) -> None:
        """Update an appliance via the HijuConn gateway."""
        data = {
            "puid": puid,
            "properties": properties,
        }
        await self._fetch_access_token()
        await self._update_gateway_appliance(
            data, retry_on_reauth=True, retry_on_randstr=True,
        )

    async def get_daily_energy_kwh(
        self,
        puid: str,
        device_type_code: str,
        device_feature_code: str,
    ) -> float | None:
        """Fetch today's energy consumption in kWh for a device.

        Returns None if the energy endpoint fails for this device.
        """
        await self._fetch_access_token()
        today = dt.date.today().isoformat()
        try:
            response = await self._request_gateway_json(
                self.gateway_energy_url,
                payload={
                    "puid": puid,
                    "statType": "day",
                    "dateStart": today,
                    "dateEnd": today,
                    "curve": "1",
                    "deviceType": device_type_code,
                    "featureCode": device_feature_code,
                },
                retry_on_reauth=True,
            )
            result_data = response.get("resultData")
            if isinstance(result_data, dict):
                return result_data.get("electricTotal")
        except (LifeConnectError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Energy fetch failed for %s: %s", puid, err)
        return None

    def _normalize_appliance_payloads(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Preserve cached statusList when upstream omits it."""
        cached = {a.device_id: a.status_list for a in self.appliances}
        if not cached:
            return self._drop_incomplete_appliance_payloads(payloads)
        result: list[dict[str, Any]] = []
        for p in payloads:
            device_id = p.get("deviceId")
            if "statusList" not in p and device_id in cached:
                _LOGGER.debug(
                    "Appliance %s payload missing statusList, using cached value",
                    device_id,
                )
                p = {**p, "statusList": cached[device_id]}
            result.append(p)
        return self._drop_incomplete_appliance_payloads(result)

    @staticmethod
    def _drop_incomplete_appliance_payloads(
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop entries that still lack a statusList after normalization."""
        complete: list[dict[str, Any]] = []
        for p in payloads:
            if "statusList" not in p:
                _LOGGER.warning(
                    "Dropping appliance %s: no statusList available",
                    p.get("deviceId"),
                )
                continue
            complete.append(p)
        return complete

    # -- Appliance requests (HijuConn gateway) --------------------------------

    async def _request_gateway_appliances_json(self, *, retry_on_reauth: bool) -> list[dict[str, Any]]:
        gateway_response = await self._request_gateway_json(
            self.gateway_device_list_url,
            payload={},
            retry_on_reauth=retry_on_reauth,
            retry_on_randstr=True,
            method="GET",
        )
        device_list = gateway_response.get("deviceList")
        if not isinstance(device_list, list):
            raise LifeConnectError(
                "Unexpected response from HijuConn gateway: missing 'deviceList'",
                endpoint=self.gateway_device_list_url,
            )
        return device_list

    # -- Auth / token management --------------------------------------------

    async def _fetch_access_token(self) -> None:
        now = dt.datetime.now()
        if self._expires is None or self._access_token is None:
            await self._initial_access_token_with_retry()
            return

        if self._expires >= now:
            return

        if self._refresh_token is None or (
            self._refresh_token_expires is not None and self._refresh_token_expires <= now
        ):
            self._reset_tokens()
            await self._initial_access_token_with_retry()
            return

        try:
            await self._refresh_access_token()
        except (LifeConnectAuthError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning(
                "ConnectLife token refresh failed, retrying full login: %s",
                err,
            )
            self._reset_tokens()
            await self._initial_access_token_with_retry()

    async def _initial_access_token_with_retry(self) -> None:
        attempts = 2
        last_error: LifeConnectError | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self._initial_access_token()
                return
            except (aiohttp.ClientError, TimeoutError) as err:
                last_error = LifeConnectError(f"Unexpected error during login: {err}")
                last_error.__cause__ = err
                if attempt == attempts:
                    break
                _LOGGER.warning(
                    "ConnectLife login attempt %d/%d failed with transport error, retrying: %s",
                    attempt,
                    attempts,
                    err,
                )
            except LifeConnectAuthError as err:
                last_error = err
                if err.status not in AUTH_TRANSIENT_STATUSES:
                    raise
                if attempt == attempts:
                    break
                _LOGGER.warning(
                    "ConnectLife login attempt %d/%d failed with transient auth error, retrying: %s",
                    attempt,
                    attempts,
                    err,
                )
            self._reset_tokens()
            await asyncio.sleep(2)

        if last_error is not None:
            raise last_error
        raise LifeConnectError("Unexpected error during login")

    async def _initial_access_token(self) -> None:
        async with self._client_session() as session:
            uid, login_token = await self._login_to_gigya(session)
            id_token = await self._fetch_jwt(session, login_token)
            code = await self._authorize(session, uid, id_token)
            await self._exchange_authorization_code(session, code)

    async def _login_to_gigya(self, session: aiohttp.ClientSession) -> tuple[str, str]:
        async with session.post(
            self.login_url,
            data={
                "loginID": self._username,
                "password": self._password,
                "APIKey": self.api_key,
            },
        ) as response:
            if response.status != 200:
                body = await self._read_response_body(response)
                raise self._response_error(
                    "Unexpected response from login: status={status}",
                    response,
                    body,
                    endpoint=self.login_url,
                    auth=True,
                )
            body = await self._json(response, endpoint=self.login_url, auth=True)
            error_code = body.get("errorCode")
            error_message = body.get("errorMessage")
            error_details = body.get("errorDetails")
            if error_code or error_message or error_details:
                raise LifeConnectAuthError(
                    f"Failed to login. Code: {error_code} Message: '{error_message}' Details: '{error_details}'"
                )

            uid = self._require_auth_field(body, "UID")
            session_info = self._require_auth_field(body, "sessionInfo")
            if "cookieValue" not in session_info:
                _LOGGER.debug("Missing 'sessionInfo.cookieValue' in response: %s", body)
                raise LifeConnectAuthError("Missing 'sessionInfo.cookieValue' in response")
            return uid, session_info["cookieValue"]

    async def _fetch_jwt(self, session: aiohttp.ClientSession, login_token: str) -> str:
        async with session.post(
            self.jwt_url,
            data={
                "APIKey": self.api_key,
                "login_token": login_token,
            },
        ) as response:
            if response.status != 200:
                body = await self._read_response_body(response)
                raise self._response_error(
                    "Unexpected response from getJWT: status={status}",
                    response,
                    body,
                    endpoint=self.jwt_url,
                    auth=True,
                )
            body = await self._json(response, endpoint=self.jwt_url, auth=True)
            return self._require_auth_field(body, "id_token")

    async def _authorize(
        self,
        session: aiohttp.ClientSession,
        uid: str,
        id_token: str,
    ) -> str:
        async with session.post(
            self.oauth2_authorize,
            json={
                "client_id": self.client_id,
                "redirect_uri": self.oauth2_redirect_uri,
                "idToken": id_token,
                "response_type": "code",
                "thirdType": "CDC",
                "thirdClientId": uid,
            },
        ) as response:
            if response.status != 200:
                body = await self._read_response_body(response)
                raise self._response_error(
                    "Unexpected response from authorize: status={status}",
                    response,
                    body,
                    endpoint=self.oauth2_authorize,
                    auth=True,
                )
            body = await self._json(response, endpoint=self.oauth2_authorize, auth=True)
            return self._require_auth_field(body, "code")

    async def _exchange_authorization_code(
        self,
        session: aiohttp.ClientSession,
        code: str,
    ) -> None:
        async with session.post(
            self.oauth2_token,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.oauth2_redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
            },
        ) as response:
            if response.status != 200:
                body = await self._read_response_body(response)
                raise self._response_error(
                    "Unexpected response from initial access token: status={status}",
                    response,
                    body,
                    endpoint=self.oauth2_token,
                    auth=True,
                )
            body = await self._json(response, endpoint=self.oauth2_token, auth=True)
            self._set_token_state(body)

    async def _refresh_access_token(self) -> None:
        async with self._client_session() as session:
            async with session.post(
                self.oauth2_token,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.oauth2_redirect_uri,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            ) as response:
                if response.status != 200:
                    body = await self._read_response_body(response)
                    raise self._response_error(
                        "Unexpected response from refreshing access token: status={status}",
                        response,
                        body,
                        endpoint=self.oauth2_token,
                        auth=True,
                    )
                body = await self._json(response, endpoint=self.oauth2_token, auth=True)
                self._set_token_state(body)

    # -- HijuConn gateway ---------------------------------------------------

    async def _update_gateway_appliance(
        self,
        data: dict[str, Any],
        *,
        retry_on_reauth: bool,
        retry_on_randstr: bool = False,
    ) -> None:
        await self._request_gateway_json(
            self.gateway_update_url,
            payload=data,
            retry_on_reauth=retry_on_reauth,
            retry_on_randstr=retry_on_randstr,
        )

    async def _request_gateway_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        retry_on_reauth: bool,
        retry_on_randstr: bool = False,
        method: str = "POST",
    ) -> dict[str, Any]:
        request_data = self._gateway_request_data(payload)

        async with self._client_session() as session:
            request = session.get if method == "GET" else session.post
            request_kwargs: dict[str, Any]
            if method == "GET":
                request_kwargs = {
                    "params": request_data,
                    "headers": {"User-Agent": self.gateway_user_agent},
                }
            else:
                request_kwargs = {
                    "json": request_data,
                    "headers": {"User-Agent": self.gateway_user_agent},
                }
            async with request(url, **request_kwargs) as response:
                if response.status != 200:
                    body = await self._read_response_body(response)
                    raise self._response_error(
                        "Unexpected response from HijuConn gateway: status={status}",
                        response,
                        body,
                        endpoint=url,
                    )
                body = await self._json(response, endpoint=url)

        gateway_response = body.get("response")
        if not isinstance(gateway_response, dict):
            raise LifeConnectError(
                "Unexpected response from HijuConn gateway: missing 'response'",
                endpoint=url,
            )

        result_code = gateway_response.get("resultCode")
        if result_code in (0, "0", None):
            return gateway_response

        error_code = gateway_response.get("errorCode")
        error_desc = gateway_response.get("errorDesc") or "Unknown gateway error"
        if retry_on_reauth and error_code == self.invalid_access_token_code:
            _LOGGER.warning("HijuConn gateway access token rejected, retrying full login")
            await self.login()
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=False,
                retry_on_randstr=retry_on_randstr,
                method=method,
            )

        if retry_on_randstr and error_code == self.randstr_check_failed_code:
            _LOGGER.warning("HijuConn gateway randStr check failed, retrying with fresh signature")
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=retry_on_reauth,
                retry_on_randstr=False,
                method=method,
            )

        error_type = LifeConnectAuthError if error_code == self.invalid_access_token_code else LifeConnectError
        raise error_type(
            f"Unexpected response from HijuConn gateway: code={error_code} description='{error_desc}'",
            endpoint=url,
        )

    # -- Token helpers ------------------------------------------------------

    def _set_token_state(self, response: dict[str, Any]) -> None:
        self._access_token = self._require_auth_field(response, "access_token")
        expires_in = int(self._require_auth_field(response, "expires_in"))
        # Renew 90 seconds before expiration.
        self._expires = dt.datetime.now() + dt.timedelta(seconds=expires_in - 90)
        self._refresh_token = response.get("refresh_token", self._refresh_token)
        self._refresh_token_expires = self._parse_refresh_token_expiry(
            response.get("refreshTokenExpiredTime")
        )

    def _reset_tokens(self) -> None:
        self._access_token = None
        self._expires = None
        self._refresh_token = None
        self._refresh_token_expires = None

    def _require_access_token(self) -> str:
        if self._access_token is None:
            raise LifeConnectAuthError("Missing 'access_token' in response")
        return self._access_token

    # -- Gateway request signing --------------------------------------------

    def _gateway_request_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        timestamp = str(int(dt.datetime.now().timestamp() * 1000))
        request_data: dict[str, Any] = {
            "accessToken": self._require_access_token(),
            "appId": GATEWAY_APP_ID,
            "appSecret": GATEWAY_APP_SECRET,
            "languageId": GATEWAY_LANGUAGE_ID,
            # MD5 of timestamp matches the vendor's mobile app protocol.
            "randStr": hashlib.md5(timestamp.encode()).hexdigest(),
            "timeStamp": timestamp,
            "timezone": GATEWAY_TIMEZONE,
            "version": GATEWAY_VERSION,
        }
        request_data.update(payload)
        request_data["sign"] = self._sign_gateway_request(request_data)
        return request_data

    @staticmethod
    def _sign_gateway_request(payload: dict[str, Any]) -> str:
        unsigned_items = []
        for key in sorted(k for k in payload if k != "sign"):
            value = payload[key]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            unsigned_items.append(f"{key}={value}")
        digest = hashlib.sha256(
            f"{'&'.join(unsigned_items)}{GATEWAY_SIGN_SUFFIX}".encode()
        ).digest()
        encrypted = GATEWAY_PUBLIC_KEY.encrypt(digest, padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    # -- HTTP helpers -------------------------------------------------------

    def _client_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=self.request_timeout)

    @staticmethod
    async def _json(
        response: aiohttp.ClientResponse,
        *,
        endpoint: str | None = None,
        auth: bool = False,
    ) -> Any:
        # response may have wrong content-type, cannot use response.json()
        text = await response.text()
        _LOGGER.debug("response: %s", text)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            error_type = LifeConnectAuthError if auth else LifeConnectError
            raise error_type(
                f"Non-JSON response from {endpoint or 'unknown'}: {text[:200]}",
                endpoint=endpoint,
            )

    @staticmethod
    async def _read_response_body(response: aiohttp.ClientResponse) -> str:
        text = await response.text()
        _LOGGER.debug("Response status code: %s", response.status)
        _LOGGER.debug("Response headers: %s", response.headers)
        _LOGGER.debug(text)
        return text

    @staticmethod
    def _require_auth_field(response: dict[str, Any], field: str) -> Any:
        if field not in response:
            _LOGGER.debug("Missing '%s' in response: %s", field, response)
            raise LifeConnectAuthError(f"Missing '{field}' in response")
        return response[field]

    @staticmethod
    def _parse_refresh_token_expiry(value: Any) -> dt.datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(float(value) / 1000)
        if isinstance(value, str):
            if value.isdigit():
                return dt.datetime.fromtimestamp(int(value) / 1000)
            try:
                return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                _LOGGER.debug("Unable to parse refreshTokenExpiredTime=%s", value)
        return None

    @staticmethod
    def _response_error(
        message_template: str,
        response: aiohttp.ClientResponse,
        body: str,
        *,
        endpoint: str,
        auth: bool = False,
    ) -> LifeConnectError:
        message = message_template.format(status=response.status)
        if body:
            _LOGGER.debug("ConnectLife error body from %s: %s", endpoint, body)
        error_type = LifeConnectAuthError if auth else LifeConnectError
        return error_type(message, status=response.status, endpoint=endpoint)
