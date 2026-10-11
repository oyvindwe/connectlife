"""ConnectLife API client for tokens from the browser OAuth2 flow.

The browser flow signs in on ``oauth.hijuconn.com/login`` (password or a social
provider, and accepting updated terms) and redirects back with an authorization
code. The caller, e.g. Home Assistant's ``config_entry_oauth2_flow``, exchanges the
code and refreshes the tokens itself; this client only asks for a valid access
token before each request.

Requests go to Hisense's third-party API (``juapi-3rd.hijuconn.com``), which uses
the same endpoint paths as the app gateway but signs requests with HMAC-SHA256
headers, and returns ``resultCode`` at the top level instead of under
``response``. The signing follows Hisense's own Home Assistant integration
(``Connectlife-LLC/HomeAssistantPlugin``, MIT).

The third-party API has no ``air_duct_energy`` or ``energyConsumptionCurve``, so
those two requests go to the app gateway, which accepts the same access token. So
does ``query_system_data``, which has only been seen on the app gateway.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import uuid
from typing import Any

from .api import ConnectLifeApi, LifeConnectAuthError, LifeConnectError

_LOGGER = logging.getLogger(__name__)

# Hisense's OAuth client for Home Assistant, as published in their
# connectlife-cloud library. It accepts the My Home Assistant redirect
# (https://my.home-assistant.io/redirect/oauth).
OAUTH2_CLIENT_ID = "9793620883275788"
OAUTH2_CLIENT_SECRET = "7h1m3gZVlILyBvIFBNmzXwoFYLhkGqG9NQd2jBzuZCqJKCTyCtYwQtXi4tVBjg9B"
# The browser login page, not the /oauth/authorize API the password login uses.
OAUTH2_AUTHORIZE_URL = "https://oauth.hijuconn.com/login"
OAUTH2_TOKEN_URL = "https://oauth.hijuconn.com/oauth/token"

JUAPI_BASE_URL = "https://juapi-3rd.hijuconn.com"

# SHA-256 digest (base64) of an empty body.
EMPTY_BODY_DIGEST = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


class OAuthConnectLifeApi(ConnectLifeApi):
    """ConnectLife API client using access tokens managed by the caller."""

    gateway_user_agent = "connectlife-oauth"

    def __init__(
        self,
        access_token: Callable[[], Awaitable[str]],
        test_server: str | None = None,
        *,
        source_id: str | None = None,
    ):
        """Initialize the client.

        ``access_token`` returns a valid access token, refreshing it first if needed.
        It is called before every request. If the API rejects the token, it is
        called once more, and a second rejection raises ``LifeConnectAuthError``:
        the caller should then run the browser flow again.

        ``source_id`` identifies this client to the API. Persist one (see
        :func:`generate_source_id`), as a new one may register a new client each time.
        """
        super().__init__("", "")
        self._get_access_token = access_token
        self.app_id = OAUTH2_CLIENT_ID
        self.app_secret = OAUTH2_CLIENT_SECRET
        self.source_id = source_id or generate_source_id()

        if test_server:
            # Requests to the app gateway go to the test server's app gateway routes.
            self.gateway_energy_url = f"{test_server}/clife-svc/pu/air_duct_energy"
            self.gateway_energy_consumption_url = f"{test_server}/clife-svc/pu/energyConsumptionCurve"
            self.gateway_system_data_url = f"{test_server}/clife-svc/pu/query_system_data"
        self._app_gateway_urls = {
            self.gateway_energy_url,
            self.gateway_energy_consumption_url,
            self.gateway_system_data_url,
        }

        base = test_server or JUAPI_BASE_URL
        self.gateway_device_list_url = f"{base}/clife-svc/pu/get_device_status_list"
        self.gateway_update_url = f"{base}/device/pu/property/set"
        self.gateway_static_data_url = f"{base}/clife-svc/pu/query_static_data"
        self.gateway_property_list_url = f"{base}/clife-svc/get_property_list"
        self.gateway_self_check_url = f"{base}/basic/self_check/info"
        self.gateway_hour_power_url = f"{base}/clife-svc/pu/get_hour_power"

    async def _fetch_access_token(self) -> None:
        self._access_token = await self._get_access_token()

    async def _request_gateway_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        retry_on_reauth: bool,
        retry_on_randstr: bool = False,
        method: str = "POST",
    ) -> dict[str, Any]:
        if url in self._app_gateway_urls:
            return await super()._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=retry_on_reauth,
                retry_on_randstr=retry_on_randstr,
                method=method,
            )
        request_data = {**payload, **self._system_parameters()}
        access_token = self._require_access_token()
        headers = {"User-Agent": self.gateway_user_agent, "Content-Type": "application/json"}

        async with self._client_session() as session:
            if method == "GET":
                # Parameters go in the query string and the token in a header.
                query = _query_string(request_data)
                request_url = f"{url}?{query}" if query else url
                headers.update(self._signing_headers(method, request_url, None))
                headers["accessToken"] = access_token
                request = session.get(request_url, headers=headers)
            else:
                body = json.dumps({**request_data, "accessToken": access_token})
                headers.update(self._signing_headers(method, url, body))
                request = session.post(url, data=body, headers=headers)

            async with request as response:
                if response.status != 200:
                    error_body = await self._read_response_body(response)
                    raise self._response_error(
                        "Unexpected response from HijuConn API: status={status}",
                        response,
                        error_body,
                        endpoint=url,
                    )
                body_json = await self._json(response, endpoint=url)

        if not isinstance(body_json, dict):
            raise LifeConnectError(
                "Unexpected response from HijuConn API: not an object", endpoint=url
            )

        result_code = body_json.get("resultCode")
        if result_code in (0, "0", None):
            return body_json

        error_code = body_json.get("errorCode", result_code)
        error_desc = body_json.get("errorDesc") or body_json.get("msg") or "Unknown API error"
        if retry_on_reauth and error_code == self.invalid_access_token_code:
            _LOGGER.warning("HijuConn API access token rejected, fetching it again")
            await self._fetch_access_token()
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=False,
                retry_on_randstr=retry_on_randstr,
                method=method,
            )
        if retry_on_randstr and error_code == self.randstr_check_failed_code:
            _LOGGER.debug("HijuConn API randStr check failed, retrying with a new one")
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=retry_on_reauth,
                retry_on_randstr=False,
                method=method,
            )
        error_type = (
            LifeConnectAuthError if error_code == self.invalid_access_token_code else LifeConnectError
        )
        raise error_type(
            f"Unexpected response from HijuConn API: code={error_code} description='{error_desc}'",
            endpoint=url,
        )

    def _system_parameters(self) -> dict[str, Any]:
        timestamp = str(int(dt.datetime.now().timestamp() * 1000))
        return {
            "timeStamp": timestamp,
            "version": "8.1",
            "languageId": "1",
            "timezone": "UTC",
            "randStr": hashlib.md5(f"{uuid.uuid1()}{timestamp}".encode()).hexdigest(),
            "appId": self.app_id,
            "sourceId": self.source_id,
            "platformId": 5,
        }

    def _signing_headers(self, method: str, url: str, body: str | None) -> dict[str, str]:
        gmt_date = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        path = re.sub(r"^https?://[^/]*", "", url)
        signing_string = (
            f"{self.app_id}\n{method} {path}\ndate: {gmt_date}\n"
            f"hi-params-encrypt: {self.app_id}\n"
        )
        signature = base64.b64encode(
            hmac.new(self.app_secret.encode(), signing_string.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "hi-params-encrypt": self.app_id,
            "Date": gmt_date,
            "Authorization": (
                f'Signature signature="{signature}", keyId="{self.app_id}",'
                f'algorithm="hmac-sha256", headers="@request-target date hi-params-encrypt"'
            ),
            "Digest": f"SHA-256={_body_digest(body)}",
        }


def generate_source_id() -> str:
    """Return a new client identifier for :class:`OAuthConnectLifeApi`."""
    digest = hashlib.md5(
        f"{uuid.uuid1().hex}{int(dt.datetime.now().timestamp() * 1000)}".encode()
    ).hexdigest()
    return f"td001002000{digest}"


def _body_digest(body: str | None) -> str:
    if not body:
        return EMPTY_BODY_DIGEST
    return base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()


def _query_string(params: dict[str, Any]) -> str:
    return "&".join(
        f"{key}={json.dumps(value) if isinstance(value, (dict, list)) else value}"
        for key, value in params.items()
    )
