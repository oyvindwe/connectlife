"""Tests for ConnectLife API auth resilience and gateway writes."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
import unittest
from unittest.mock import patch

import aiohttp

from connectlife import api as api_module
from connectlife.api import (
    BAPI_APPLIANCES_TIMEOUT,
    ConnectLifeApi,
    DEFAULT_OAUTH_REDIRECT_URI,
    GATEWAY_DEVICE_LIST_URL,
    GATEWAY_UPDATE_URL,
    LEGACY_OAUTH_PROFILE,
    LifeConnectAuthError,
    LifeConnectError,
    OFFICIAL_OAUTH_PROFILE,
)


class FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def json(self) -> Any:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeSession:
    """Minimal aiohttp ClientSession stand-in with scripted responses."""

    def __init__(self, requests: list[tuple[str, str, FakeResponse | Exception]]) -> None:
        self._requests = requests

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url)

    def _next(self, method: str, url: str) -> FakeResponse:
        if not self._requests:
            raise AssertionError(f"Unexpected {method} request to {url}")
        expected_method, expected_url, response = self._requests.pop(0)
        if expected_method != method or expected_url != url:
            raise AssertionError(
                f"Expected {expected_method} {expected_url}, got {method} {url}"
            )
        if isinstance(response, Exception):
            raise response
        return response


class FakeClientSessionFactory:
    """Factory returning fake sessions that share one scripted request queue."""

    def __init__(self, requests: list[tuple[str, str, FakeResponse | Exception]]) -> None:
        self._requests = requests

    def __call__(self, *args: Any, **kwargs: Any) -> FakeSession:
        return FakeSession(self._requests)


def _successful_login_requests(
    api: ConnectLifeApi,
    *,
    access_token: str = "new-access-token",
    refresh_token: str = "new-refresh-token",
) -> list[tuple[str, str, FakeResponse]]:
    """Return the sequence of requests for a successful 4-step OAuth2 login."""
    return [
        (
            "POST",
            api.login_url,
            FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
        ),
        ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
        ("POST", api.oauth2_authorize, FakeResponse(200, {"code": "auth-code"})),
        (
            "POST",
            api.oauth2_token,
            FakeResponse(200, {
                "access_token": access_token,
                "expires_in": 3600,
                "refresh_token": refresh_token,
                "refreshTokenExpiredTime": 4_102_444_800_000,
            }),
        ),
    ]


class TestRefreshFallback(unittest.IsolatedAsyncioTestCase):
    """Token refresh failure should fall back to a full login."""

    async def test_refresh_failure_falls_back_to_full_login(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "expired-token"
        api._refresh_token = "expired-refresh"
        api._expires = dt.datetime.now() - dt.timedelta(seconds=1)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", api.oauth2_token, FakeResponse(500, {"error": "temporary failure"})),
            *_successful_login_requests(api),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api._fetch_access_token()

        self.assertEqual(api._access_token, "new-access-token")
        self.assertEqual(api._refresh_token, "new-refresh-token")
        self.assertGreater(api._expires, dt.datetime.now())
        self.assertFalse(requests)


class TestLoginRetry(unittest.IsolatedAsyncioTestCase):
    """Transient auth errors during initial login should be retried."""

    async def test_initial_login_retries_after_transient_auth_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", api.login_url, FakeResponse(500, {"error": "upstream login error"})),
            *_successful_login_requests(api),
        ]

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            patch.object(api_module.asyncio, "sleep", return_value=None),
        ):
            await api.login()

        self.assertEqual(api._access_token, "new-access-token")
        self.assertFalse(requests)

    async def test_non_transient_auth_error_raises_immediately(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", api.login_url, FakeResponse(200, {
                "errorCode": 403042,
                "errorMessage": "Invalid LoginID",
                "errorDetails": "invalid loginID or password",
            })),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectAuthError):
                await api.login()

    async def test_auth_401_does_not_fall_back_to_official_oauth_profile(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            (
                "POST",
                api.login_url,
                FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
            ),
            ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
            ("POST", api.oauth2_authorize, FakeResponse(401, {"error": "unauthorized"})),
        ]

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            patch.object(api_module.asyncio, "sleep", return_value=None),
        ):
            with self.assertRaises(LifeConnectAuthError) as ctx:
                await api.login()

        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(requests)

    async def test_initial_login_falls_back_to_official_oauth_profile(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            (
                "POST",
                api.login_url,
                FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
            ),
            ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
            ("POST", api.oauth2_authorize, FakeResponse(500, {"error": "legacy oauth unavailable"})),
            (
                "POST",
                api.login_url,
                FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
            ),
            ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
            ("POST", api.oauth2_authorize, FakeResponse(500, {"error": "legacy oauth unavailable"})),
            (
                "POST",
                api.login_url,
                FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
            ),
            ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
            ("POST", api.oauth2_authorize, FakeResponse(200, {"code": "auth-code"})),
            (
                "POST",
                api.oauth2_token,
                FakeResponse(200, {
                    "access_token": "official-access-token",
                    "expires_in": 3600,
                    "refresh_token": "official-refresh-token",
                }),
            ),
        ]

        authorize_payloads: list[dict[str, Any]] = []
        token_payloads: list[dict[str, Any]] = []

        def record_post(self: FakeSession, url: str, **kwargs: Any) -> FakeResponse:
            if url == api.oauth2_authorize and "json" in kwargs:
                authorize_payloads.append(kwargs["json"])
            if url == api.oauth2_token and "data" in kwargs:
                token_payloads.append(kwargs["data"])
            return FakeSession._next(self, "POST", url)

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            patch.object(FakeSession, "post", new=record_post),
            patch.object(api_module.asyncio, "sleep", return_value=None),
        ):
            await api.login()

        self.assertEqual(api._access_token, "official-access-token")
        self.assertEqual(api._refresh_token, "official-refresh-token")
        self.assertEqual(authorize_payloads[0]["client_id"], LEGACY_OAUTH_PROFILE.client_id)
        self.assertEqual(authorize_payloads[0]["redirect_uri"], DEFAULT_OAUTH_REDIRECT_URI)
        self.assertEqual(authorize_payloads[1]["client_id"], LEGACY_OAUTH_PROFILE.client_id)
        self.assertEqual(authorize_payloads[1]["redirect_uri"], DEFAULT_OAUTH_REDIRECT_URI)
        self.assertEqual(authorize_payloads[2]["client_id"], OFFICIAL_OAUTH_PROFILE.client_id)
        self.assertEqual(authorize_payloads[2]["redirect_uri"], DEFAULT_OAUTH_REDIRECT_URI)
        self.assertEqual(token_payloads[0]["client_id"], OFFICIAL_OAUTH_PROFILE.client_id)
        self.assertEqual(token_payloads[0]["client_secret"], OFFICIAL_OAUTH_PROFILE.client_secret)
        self.assertFalse(requests)

    async def test_login_uses_custom_redirect_uri_when_provided(self) -> None:
        api = ConnectLifeApi(
            "user@example.com",
            "secret",
            oauth_redirect_uri="https://example.com/oauth/callback",
        )

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            (
                "POST",
                api.login_url,
                FakeResponse(200, {"UID": "uid-1", "sessionInfo": {"cookieValue": "login-token"}}),
            ),
            ("POST", api.jwt_url, FakeResponse(200, {"id_token": "jwt-token"})),
            ("POST", api.oauth2_authorize, FakeResponse(200, {"code": "auth-code"})),
            (
                "POST",
                api.oauth2_token,
                FakeResponse(200, {
                    "access_token": "custom-access-token",
                    "expires_in": 3600,
                    "refresh_token": "custom-refresh-token",
                }),
            ),
        ]

        authorize_payloads: list[dict[str, Any]] = []
        token_payloads: list[dict[str, Any]] = []

        def record_post(self: FakeSession, url: str, **kwargs: Any) -> FakeResponse:
            if url == api.oauth2_authorize and "json" in kwargs:
                authorize_payloads.append(kwargs["json"])
            if url == api.oauth2_token and "data" in kwargs:
                token_payloads.append(kwargs["data"])
            return FakeSession._next(self, "POST", url)

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            patch.object(FakeSession, "post", new=record_post),
        ):
            await api.login()

        self.assertEqual(authorize_payloads[0]["redirect_uri"], "https://example.com/oauth/callback")
        self.assertEqual(token_payloads[0]["redirect_uri"], "https://example.com/oauth/callback")


class TestAppliancesReauth(unittest.IsolatedAsyncioTestCase):
    """Transient failures on appliance requests should trigger re-auth and retry."""

    async def test_appliances_request_reauths_after_transient_server_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", api.appliances_url, FakeResponse(500, {"error": "backend unavailable"})),
            *_successful_login_requests(api),
            ("GET", api.appliances_url, FakeResponse(200, [{"deviceId": "device-1"}])),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertEqual(api._access_token, "new-access-token")
        self.assertFalse(requests)

    async def test_appliances_request_falls_back_to_gateway_after_bapi_failures(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("GET", api.appliances_url, FakeResponse(500, {"error": "backend unavailable"})),
            *_successful_login_requests(api, access_token="replacement-access-token", refresh_token="replacement-refresh-token"),
            ("GET", api.appliances_url, FakeResponse(500, {"error": "backend unavailable"})),
            (
                "GET",
                GATEWAY_DEVICE_LIST_URL,
                FakeResponse(200, {"response": {"resultCode": 0, "deviceList": [{"deviceId": "device-1"}]}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertEqual(api._access_token, "replacement-access-token")
        self.assertFalse(requests)

    async def test_appliances_request_falls_back_to_gateway_after_timeout(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("GET", api.appliances_url, TimeoutError()),
            (
                "GET",
                GATEWAY_DEVICE_LIST_URL,
                FakeResponse(200, {"response": {"resultCode": 0, "deviceList": [{"deviceId": "device-1"}]}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertFalse(requests)

    async def test_appliance_list_gateway_fallback_uses_get(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("GET", api.appliances_url, TimeoutError()),
            (
                "GET",
                GATEWAY_DEVICE_LIST_URL,
                FakeResponse(200, {"response": {"resultCode": 0, "deviceList": [{"deviceId": "device-1"}]}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertFalse(requests)

    async def test_appliances_request_does_not_retry_on_403(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", api.appliances_url, FakeResponse(403, {"error": "forbidden"})),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError) as ctx:
                await api.get_appliances_json()

        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(requests)

    async def test_appliances_request_401_reauths_once_without_gateway_fallback(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("GET", api.appliances_url, FakeResponse(401, {"error": "unauthorized"})),
            *_successful_login_requests(api, access_token="replacement-access-token", refresh_token="replacement-refresh-token"),
            ("GET", api.appliances_url, FakeResponse(401, {"error": "unauthorized"})),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError) as ctx:
                await api.get_appliances_json()

        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(api._access_token, "replacement-access-token")
        self.assertFalse(requests)

    def test_bapi_appliance_timeout_is_shorter_than_global_timeout(self) -> None:
        self.assertLess(BAPI_APPLIANCES_TIMEOUT.total, ConnectLifeApi.request_timeout.total)


class TestGatewayWrites(unittest.IsolatedAsyncioTestCase):
    """Appliance updates should try the gateway first, then fall back to bapi."""

    async def test_update_falls_back_to_bapi_on_gateway_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {
                    "response": {"resultCode": 1, "errorCode": 101005, "errorDesc": "randStr check fail."},
                }),
            ),
            ("POST", api.appliances_url, FakeResponse(200, {"ok": True})),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api.update_appliance("puid-1", {"t_temp": "22"})

        self.assertFalse(requests)

    async def test_update_succeeds_via_gateway(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {"response": {"resultCode": 0}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api.update_appliance("puid-1", {"t_temp": "22"})

        self.assertFalse(requests)

    async def test_update_reauths_on_gateway_invalid_token(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "expired-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {
                    "response": {"resultCode": 1, "errorCode": 100026, "errorDesc": "invalid access token"},
                }),
            ),
            *_successful_login_requests(api),
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {"response": {"resultCode": 0}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api.update_appliance("puid-1", {"t_temp": "22"})

        self.assertEqual(api._access_token, "new-access-token")
        self.assertFalse(requests)


class TestRefreshTokenExpiry(unittest.IsolatedAsyncioTestCase):
    """Expired refresh token should trigger a full login instead of refresh."""

    async def test_expired_refresh_token_triggers_full_login(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "expired-access-token"
        api._refresh_token = "expired-refresh-token"
        api._expires = dt.datetime.now() - dt.timedelta(seconds=1)
        api._refresh_token_expires = dt.datetime.now() - dt.timedelta(seconds=1)

        requests: list[tuple[str, str, FakeResponse]] = [
            *_successful_login_requests(api),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api._fetch_access_token()

        self.assertEqual(api._access_token, "new-access-token")
        self.assertEqual(api._refresh_token, "new-refresh-token")
        self.assertFalse(requests)


class TestTransportErrorRetry(unittest.IsolatedAsyncioTestCase):
    """Transport errors during login should be retried once."""

    async def test_login_retries_after_transport_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        factory = FakeClientSessionFactory([])

        call_count = 0
        original_initial = api._initial_access_token

        async def flaky_initial() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientConnectionError("connection reset")
            # On second attempt, do a real (mocked) login
            await original_initial()

        requests: list[tuple[str, str, FakeResponse]] = [
            *_successful_login_requests(api),
        ]

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            patch.object(api, "_initial_access_token", side_effect=flaky_initial),
            patch.object(api_module.asyncio, "sleep", return_value=None),
        ):
            await api.login()

        self.assertEqual(call_count, 2)
        self.assertEqual(api._access_token, "new-access-token")

    async def test_login_raises_after_exhausting_transport_retries(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        async def always_fail() -> None:
            raise aiohttp.ClientConnectionError("connection refused")

        with (
            patch.object(api, "_initial_access_token", side_effect=always_fail),
            patch.object(api_module.asyncio, "sleep", return_value=None),
        ):
            with self.assertRaises(LifeConnectAuthError) as ctx:
                await api.login()
            self.assertIn("connection refused", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, aiohttp.ClientConnectionError)


class TestAuthenticate(unittest.IsolatedAsyncioTestCase):
    """authenticate() should return True on success and False on auth failure."""

    async def test_authenticate_returns_true_on_success(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse]] = [
            *_successful_login_requests(api),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.authenticate()

        self.assertTrue(result)
        self.assertEqual(api._access_token, "new-access-token")

    async def test_authenticate_returns_false_on_auth_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", api.login_url, FakeResponse(200, {
                "errorCode": 403042,
                "errorMessage": "Invalid LoginID",
                "errorDetails": "invalid loginID or password",
            })),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.authenticate()

        self.assertFalse(result)


class TestExceptionHierarchy(unittest.TestCase):
    """LifeConnectAuthError should be a subclass of LifeConnectError."""

    def test_auth_error_is_subclass(self) -> None:
        self.assertTrue(issubclass(LifeConnectAuthError, LifeConnectError))

    def test_error_has_status_and_endpoint(self) -> None:
        err = LifeConnectError("test", status=500, endpoint="/foo")
        self.assertEqual(err.status, 500)
        self.assertEqual(err.endpoint, "/foo")


if __name__ == "__main__":
    unittest.main()
