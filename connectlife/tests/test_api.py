"""Tests for ConnectLife API auth resilience and gateway writes."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
import unittest
from unittest.mock import patch

from connectlife import api as api_module
from connectlife.api import (
    ConnectLifeApi,
    GATEWAY_UPDATE_URL,
    LifeConnectAuthError,
    LifeConnectError,
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

    def __init__(self, requests: list[tuple[str, str, FakeResponse]]) -> None:
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
        return response


class FakeClientSessionFactory:
    """Factory returning fake sessions that share one scripted request queue."""

    def __init__(self, requests: list[tuple[str, str, FakeResponse]]) -> None:
        self._requests = requests

    def __call__(self, *args: Any, **kwargs: Any) -> FakeSession:
        return FakeSession(self._requests)


def _successful_login_requests(api: ConnectLifeApi) -> list[tuple[str, str, FakeResponse]]:
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
                "access_token": "new-access-token",
                "expires_in": 3600,
                "refresh_token": "new-refresh-token",
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