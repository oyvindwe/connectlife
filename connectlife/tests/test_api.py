"""Tests for ConnectLife API auth resilience and gateway operations."""

from __future__ import annotations

from collections.abc import Sequence
import datetime as dt
import json
from typing import Any, cast
import unittest
from unittest.mock import patch

import aiohttp

from connectlife import api as api_module
from connectlife.api import (
    AirDuctEnergy,
    ConnectLifeApi,
    EnergyConsumption,
    GATEWAY_DEVICE_LIST_URL,
    GATEWAY_ENERGY_CONSUMPTION_URL,
    GATEWAY_ENERGY_URL,
    GATEWAY_INVALID_ACCESS_TOKEN,
    GATEWAY_PROPERTY_LIST_URL,
    GATEWAY_RANDSTR_CHECK_FAILED,
    GATEWAY_STATIC_DATA_URL,
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

    def __init__(self, requests: Sequence[tuple[str, str, FakeResponse | Exception]]) -> None:
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
        # Consumes the shared queue in place; callers assert it drains to empty.
        queue = cast("list[tuple[str, str, FakeResponse | Exception]]", self._requests)
        expected_method, expected_url, response = queue.pop(0)
        if expected_method != method or expected_url != url:
            raise AssertionError(
                f"Expected {expected_method} {expected_url}, got {method} {url}"
            )
        if isinstance(response, Exception):
            raise response
        return response


class FakeClientSessionFactory:
    """Factory returning fake sessions that share one scripted request queue."""

    def __init__(self, requests: Sequence[tuple[str, str, FakeResponse | Exception]]) -> None:
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


def _appliance_payload(
    *,
    device_id: str = "device-1",
    status_list: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a minimal appliance payload."""
    payload = {
        "wifiId": "wifi-1",
        "deviceId": device_id,
        "puid": "puid-1",
        "deviceNickName": "Kitchen AC",
        "deviceFeatureCode": "009-100",
        "deviceFeatureName": "Air Conditioner",
        "deviceTypeCode": "009",
        "deviceTypeName": "Air Conditioner",
        "role": 1,
        "roomId": 1,
        "roomName": "Kitchen",
        "offlineState": 0,
        "seq": 1,
        "bindTime": 0,
        "useTime": 0,
        "createTime": 0,
    }
    if status_list is not None:
        payload["statusList"] = status_list
    return payload


def _gateway_device_list_response(
    device_list: list[dict[str, Any]],
) -> FakeResponse:
    """Return a successful gateway device list response."""
    return FakeResponse(200, {"response": {"resultCode": 0, "deviceList": device_list}})


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

    async def test_non_transient_auth_error_raises_without_retry(self) -> None:
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


class TestGetAppliances(unittest.IsolatedAsyncioTestCase):
    """Appliance list fetching via the HijuConn gateway."""

    async def test_get_appliances_drops_incomplete_payload_on_cold_start(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([_appliance_payload()])),
        ]

        with (
            patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)),
            self.assertLogs("connectlife.api", level="WARNING") as captured,
        ):
            appliances = await api.get_appliances()

        self.assertEqual(len(appliances), 0)
        self.assertIn("no statusList available", captured.output[0])
        self.assertFalse(requests)

    async def test_get_appliances_drops_new_device_without_status_list_while_keeping_cached(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        # First fetch — device-1 has statusList
        first_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(device_id="device-1", status_list={"t_temp": "22"}),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(first_requests)):
            await api.get_appliances()

        # Second fetch — device-1 missing statusList (use cache), device-2 new without statusList (drop)
        second_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(device_id="device-1"),
                _appliance_payload(device_id="device-2"),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(second_requests)):
            appliances = await api.get_appliances()

        self.assertEqual(len(appliances), 1)
        self.assertEqual(appliances[0].device_id, "device-1")
        self.assertEqual(appliances[0].status_list, {"t_temp": 22})

    async def test_get_appliances_preserves_cached_datetime_status_values(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        # First fetch with a datetime-formatted status value
        first_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(status_list={"t_temp": "22", "last_run": "2026/03/31T12:00:00"}),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(first_requests)):
            appliances = await api.get_appliances()
        self.assertIsInstance(appliances[0].status_list["last_run"], dt.datetime)

        # Second fetch without statusList — should use cached value including datetime
        second_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([_appliance_payload()])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(second_requests)):
            appliances = await api.get_appliances()

        self.assertEqual(len(appliances), 1)
        self.assertEqual(appliances[0].status_list["t_temp"], 22)
        self.assertIsInstance(appliances[0].status_list["last_run"], dt.datetime)

    async def test_get_appliances_preserves_cached_status_list_when_missing(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        # First fetch with statusList present
        first_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(status_list={"t_temp": "22"}),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(first_requests)):
            appliances = await api.get_appliances()
        self.assertEqual(appliances[0].status_list, {"t_temp": 22})

        # Second fetch without statusList — should use cached value
        second_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([_appliance_payload()])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(second_requests)):
            appliances = await api.get_appliances()

        self.assertEqual(len(appliances), 1)
        self.assertEqual(appliances[0].status_list, {"t_temp": 22})

    async def test_get_appliances_keeps_fresh_status_list_when_present(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        # First fetch
        first_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(status_list={"t_temp": "22"}),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(first_requests)):
            await api.get_appliances()

        # Second fetch with updated statusList
        second_requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([
                _appliance_payload(status_list={"t_temp": "25"}),
            ])),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(second_requests)):
            appliances = await api.get_appliances()

        self.assertEqual(appliances[0].status_list, {"t_temp": 25})

    async def test_get_appliances_reauths_on_invalid_token(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "GET",
                GATEWAY_DEVICE_LIST_URL,
                FakeResponse(200, {
                    "response": {"resultCode": 1, "errorCode": 100026, "errorDesc": "invalid access token"},
                }),
            ),
            *_successful_login_requests(api),
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([{"deviceId": "device-1"}])),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertEqual(api._access_token, "new-access-token")
        self.assertFalse(requests)

    async def test_get_appliances_uses_get_method(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([{"deviceId": "device-1"}])),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertFalse(requests)


class TestGatewayErrors(unittest.IsolatedAsyncioTestCase):
    """Gateway response errors should raise descriptive exceptions."""

    async def test_get_appliances_raises_on_missing_response_key(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, FakeResponse(200, {"unexpected": "body"})),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError) as ctx:
                await api.get_appliances_json()

        self.assertIn("missing 'response'", str(ctx.exception))

    async def test_get_appliances_raises_lifeconnect_error_for_html_body(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            ("GET", GATEWAY_DEVICE_LIST_URL, FakeResponse(200, "<html>Bad Gateway</html>")),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError) as ctx:
                await api.get_appliances_json()

        self.assertIn("Non-JSON response", str(ctx.exception))

    async def test_login_raises_auth_error_for_html_body(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")

        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", api.login_url, FakeResponse(200, "<html>Service Unavailable</html>")),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectAuthError) as ctx:
                await api.login()

        self.assertIn("Non-JSON response", str(ctx.exception))


def _cached_api() -> ConnectLifeApi:
    api = ConnectLifeApi("user@example.com", "secret")
    api._access_token = "cached-access-token"
    api._expires = dt.datetime.now() + dt.timedelta(minutes=5)
    return api


class TestAirDuctEnergy(unittest.IsolatedAsyncioTestCase):
    """air_duct_energy endpoint (AC) via get_air_duct_energy."""

    async def test_returns_parsed_result(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 0,
                    "resultData": {
                        "electricTotal": 1.23,
                        "costTotal": "0.50",
                        "durationTotal": 30,
                        "electricCurve": {"0": "0.00"},
                    },
                }}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_air_duct_energy("puid-1", "009", "100")

        assert result is not None
        self.assertIsInstance(result, AirDuctEnergy)
        self.assertEqual(result.electric_total, 1.23)
        self.assertEqual(result.cost_total, 0.50)  # parsed from "0.50"
        self.assertEqual(result.duration_total, 30.0)
        self.assertEqual(result.stat_type, "day")
        self.assertEqual(result.date_start, result.date_end)  # day range
        self.assertFalse(requests)

    async def test_returns_none_on_timeout(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("POST", GATEWAY_ENERGY_URL, TimeoutError()),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            self.assertIsNone(await api.get_air_duct_energy("puid-1", "009", "100"))

    async def test_returns_none_on_missing_result_data(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            ("POST", GATEWAY_ENERGY_URL, FakeResponse(200, {"response": {"resultCode": 0}})),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            self.assertIsNone(await api.get_air_duct_energy("puid-1", "009", "100"))

    async def test_returns_none_on_gateway_error(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1, "errorCode": 999, "errorDesc": "energy not available",
                }}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            self.assertIsNone(await api.get_air_duct_energy("puid-1", "009", "100"))

    async def test_retries_on_randstr_failure(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1,
                    "errorCode": GATEWAY_RANDSTR_CHECK_FAILED,
                    "errorDesc": "randStr check fail.",
                }}),
            ),
            (
                "POST",
                GATEWAY_ENERGY_URL,
                FakeResponse(200, {"response": {"resultCode": 0, "resultData": {"electricTotal": 1.5}}}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_air_duct_energy("puid-1", "009", "100")
        assert result is not None
        self.assertEqual(result.electric_total, 1.5)
        self.assertFalse(requests)  # failure + retry both consumed

    async def test_rejects_invalid_stat_type(self) -> None:
        api = _cached_api()
        with self.assertRaises(ValueError):
            await api.get_air_duct_energy("puid-1", "009", "100", stat_type="decade")

    async def test_auth_failure_raises_without_relogin(self) -> None:
        # A rejected token must NOT trigger a full re-login per call (storm guard);
        # it raises LifeConnectAuthError after a single request.
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1,
                    "errorCode": GATEWAY_INVALID_ACCESS_TOKEN,
                    "errorDesc": "invalid access token",
                }}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectAuthError):
                await api.get_air_duct_energy("puid-1", "009", "100")
        self.assertFalse(requests)  # single request, no re-login retry


class TestEnergyConsumption(unittest.IsolatedAsyncioTestCase):
    """energyConsumptionCurve endpoint (appliances) via get_energy_consumption_curve."""

    async def test_parses_all_fields(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_CONSUMPTION_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 0,
                    "type": "week",
                    "resultData": {
                        "electricUsage": "4.00",
                        "waterUsage": "55.00",
                        "normElectricUsage": "4.00",
                        "normWaterUsage": "55.00",
                        "runTimes": "11.00",
                        "cycles": 5,
                        "electricCurve": {"2026-05-25": "1.0"},
                        "waterCurve": {"2026-05-25": "11.0"},
                        "energyPeriod": [{"dateStart": "2026-05-25", "eleUsage": "4.00"}],
                    },
                }}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_energy_consumption_curve("puid-1", "015", "dishwasher-60.2")

        assert result is not None
        self.assertIsInstance(result, EnergyConsumption)
        self.assertEqual(result.electric_total, 4.0)  # from electricUsage
        self.assertEqual(result.water_total, 55.0)
        self.assertEqual(result.run_time, 11.0)
        self.assertEqual(result.cycles, 5)
        self.assertEqual(result.stat_type, "week")
        self.assertEqual(len(result.electric_curve or {}), 1)
        self.assertEqual(len(result.energy_period or []), 1)
        self.assertFalse(requests)

    async def test_rejects_day_stat_type(self) -> None:
        # day is not supported by this endpoint; must raise before any request
        api = _cached_api()
        with self.assertRaises(ValueError):
            await api.get_energy_consumption_curve("puid-1", "015", "dishwasher-60.2", stat_type="day")

    async def test_retries_on_randstr_failure(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_ENERGY_CONSUMPTION_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1,
                    "errorCode": GATEWAY_RANDSTR_CHECK_FAILED,
                    "errorDesc": "randStr check fail.",
                }}),
            ),
            (
                "POST",
                GATEWAY_ENERGY_CONSUMPTION_URL,
                FakeResponse(200, {"response": {"resultCode": 0, "resultData": {"electricUsage": "2.0"}}}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_energy_consumption_curve("puid-1", "015", "dishwasher-60.2")
        assert result is not None
        self.assertEqual(result.electric_total, 2.0)
        self.assertFalse(requests)


class TestCapabilityProbes(unittest.IsolatedAsyncioTestCase):
    """query_static_data (per puid) and get_property_list (per feature code)."""

    async def test_query_static_data_returns_raw_response(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_STATIC_DATA_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 0,
                    "data": {"f_humidity": "1"},
                }}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.query_static_data("puid-1")
        self.assertEqual(result["data"], {"f_humidity": "1"})
        self.assertFalse(requests)

    async def test_get_property_list_is_a_get_request(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "GET",
                GATEWAY_PROPERTY_LIST_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 0,
                    "properties": ["t_temp", "t_up_down"],
                }}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_property_list("009", "104")
        self.assertEqual(result["properties"], ["t_temp", "t_up_down"])
        self.assertFalse(requests)

    async def test_gateway_error_propagates(self) -> None:
        api = _cached_api()
        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_STATIC_DATA_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1, "errorCode": 999, "errorDesc": "not supported",
                }}),
            ),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError):
                await api.query_static_data("puid-1")


class TestRandStrRetry(unittest.IsolatedAsyncioTestCase):
    """randStr check failures should be retried with a fresh signature."""

    async def test_get_appliances_retries_on_randstr_failure(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "GET",
                GATEWAY_DEVICE_LIST_URL,
                FakeResponse(200, {"response": {
                    "resultCode": 1,
                    "errorCode": GATEWAY_RANDSTR_CHECK_FAILED,
                    "errorDesc": "randStr check fail.",
                }}),
            ),
            ("GET", GATEWAY_DEVICE_LIST_URL, _gateway_device_list_response([{"deviceId": "device-1"}])),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()

        self.assertEqual(result, [{"deviceId": "device-1"}])
        self.assertFalse(requests)


class TestGatewayWrites(unittest.IsolatedAsyncioTestCase):
    """Appliance updates via the HijuConn gateway."""

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

    async def test_update_retries_on_randstr_failure(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {
                    "response": {"resultCode": 1, "errorCode": GATEWAY_RANDSTR_CHECK_FAILED, "errorDesc": "randStr check fail."},
                }),
            ),
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {"response": {"resultCode": 0}}),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api.update_appliance("puid-1", {"t_temp": "22"})

        self.assertFalse(requests)

    async def test_update_reauths_on_invalid_token(self) -> None:
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

    async def test_update_raises_on_unknown_gateway_error(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "cached-access-token"
        api._expires = dt.datetime.now() + dt.timedelta(minutes=5)

        requests: list[tuple[str, str, FakeResponse]] = [
            (
                "POST",
                GATEWAY_UPDATE_URL,
                FakeResponse(200, {
                    "response": {"resultCode": 1, "errorCode": 999, "errorDesc": "unknown error"},
                }),
            ),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            with self.assertRaises(LifeConnectError):
                await api.update_appliance("puid-1", {"t_temp": "22"})

        self.assertFalse(requests)


class TestRefreshTransportError(unittest.IsolatedAsyncioTestCase):
    """Transport errors during token refresh should fall back to full login."""

    async def test_refresh_transport_error_falls_back_to_full_login(self) -> None:
        api = ConnectLifeApi("user@example.com", "secret")
        api._access_token = "expired-token"
        api._refresh_token = "valid-refresh"
        api._expires = dt.datetime.now() - dt.timedelta(seconds=1)

        requests: list[tuple[str, str, FakeResponse | Exception]] = [
            ("POST", api.oauth2_token, TimeoutError()),
            *_successful_login_requests(api),
        ]

        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api._fetch_access_token()

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
            with self.assertRaises(LifeConnectError) as ctx:
                await api.login()
            self.assertNotIsInstance(ctx.exception, LifeConnectAuthError)
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
