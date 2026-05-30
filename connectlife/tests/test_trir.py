"""Tests for the TRIR (Russia/CIS) backend.

These verify the TRIR-specific behaviour: request signing, the HENC transport
envelope, device-payload normalization, token-state parsing, and the
login/device-list/update/refresh flows against a scripted session.

They assert internal consistency and pin regression vectors; they cannot prove
the signing/transport match the real Hisense server (only a live TRIR account
can), but since live login is known to succeed, these guard against regressions.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import unittest
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from connectlife import api as api_module
from connectlife.api import LifeConnectAuthError
from connectlife.trir import (
    TRIR_AES_IV,
    TRIR_AES_KEY,
    TRIR_TRANSPORT_MAGIC,
    TrirConnectLifeApi,
)


# -- Scripted session (TRIR replies are wrapped in {"response": {...}}) ------

class FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def json(self) -> Any:
        return json.loads(self._payload) if isinstance(self._payload, str) else self._payload


class FakeSession:
    """Scripted aiohttp ClientSession stand-in."""

    def __init__(self, requests: list[tuple[str, str, Any]]) -> None:
        self._requests = requests

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url)

    def _next(self, method: str, url: str) -> FakeResponse:
        if not self._requests:
            raise AssertionError(f"Unexpected {method} request to {url}")
        expected_method, expected_url, response = self._requests.pop(0)
        if (expected_method, expected_url) != (method, url):
            raise AssertionError(f"Expected {expected_method} {expected_url}, got {method} {url}")
        if isinstance(response, Exception):
            raise response
        return response


class FakeClientSessionFactory:
    """Factory returning fake sessions sharing one scripted request queue."""

    def __init__(self, requests: list[tuple[str, str, Any]]) -> None:
        self._requests = requests

    def __call__(self, *args: Any, **kwargs: Any) -> FakeSession:
        return FakeSession(self._requests)


def _envelope(response: dict[str, Any]) -> FakeResponse:
    """Wrap a gateway response body in the TRIR transport envelope."""
    return FakeResponse(200, {"response": response, "signatureServer": "test"})


def _token_response(
    access_token: str = "access-token",
    refresh_token: str = "refresh-token",
) -> FakeResponse:
    return _envelope({
        "resultCode": 0,
        "customerId": "12345678901234567",
        "accessToken": access_token,
        "accessTokenCreateTime": 0,
        "accessTokenExpiredTime": 86400,
        "refreshToken": refresh_token,
        "refreshTokenExpiredTime": 2592000,
    })


def _raw_trir_device(status_list: dict[str, str] | None = None) -> dict[str, Any]:
    """A device as returned raw by /br/getDeviceTabList (featureCode, bindDate)."""
    return {
        "deviceTypeCode": "009",
        "featureCode": "109",
        "deviceFeatureName": None,
        "deviceNickName": "AC",
        "deviceId": "dev-1",
        "puid": "puid-1",
        "wifiId": "wifi-1",
        "role": "1",
        "roomId": "1",
        "roomName": "Room",
        "offlineState": 1,
        "bindDate": "1745691034131",
        "useTime": "1745691034087",
        "statusList": status_list if status_list is not None else {"t_power": "1", "t_temp": "21"},
    }


# -- Pure helpers ------------------------------------------------------------

class TestSigning(unittest.TestCase):
    def setUp(self) -> None:
        self.api = TrirConnectLifeApi("user@example.ru", "secret", platform="android")

    def test_sign_matches_known_vector(self) -> None:
        # Regression vector for the android app secret + vendor secret.
        payload = {
            "appId": self.api.app_id,
            "timeStamp": "1700000000000",
            "loginName": "user@example.ru",
            "accessToken": None,
        }
        self.assertEqual(
            self.api._sign_gateway_request(payload),
            "2cc60bc1675657f08d5b03e1d49fae6231325480",
        )

    def test_sign_drops_falsy_fields(self) -> None:
        # accessToken=None (and other falsy values) must not affect the signature.
        with_none = {"appId": self.api.app_id, "timeStamp": "1", "accessToken": None, "x": ""}
        without = {"appId": self.api.app_id, "timeStamp": "1"}
        self.assertEqual(
            self.api._sign_gateway_request(with_none),
            self.api._sign_gateway_request(without),
        )

    def test_sign_excludes_existing_sign_field(self) -> None:
        base = {"appId": self.api.app_id, "timeStamp": "1"}
        signed = {**base, "sign": "stale"}
        self.assertEqual(
            self.api._sign_gateway_request(base),
            self.api._sign_gateway_request(signed),
        )

    def test_platforms_sign_differently(self) -> None:
        # Different app secrets must yield different signatures.
        android = TrirConnectLifeApi("u", "p", platform="android")
        ios = TrirConnectLifeApi("u", "p", platform="ios")
        payload = {"appId": "x", "timeStamp": "1"}
        self.assertNotEqual(
            android._sign_gateway_request(payload),
            ios._sign_gateway_request(payload),
        )


class TestTransport(unittest.TestCase):
    def test_encrypt_transport_round_trips(self) -> None:
        plaintext = json.dumps({"loginName": "user@example.ru", "x": 1})
        encoded = TrirConnectLifeApi._encrypt_transport(plaintext)
        self.assertTrue(encoded.startswith(TRIR_TRANSPORT_MAGIC))

        ciphertext = base64.b64decode(encoded[len(TRIR_TRANSPORT_MAGIC):])
        decryptor = Cipher(algorithms.AES(TRIR_AES_KEY), modes.CBC(TRIR_AES_IV)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        decoded = (unpadder.update(padded) + unpadder.finalize()).decode()
        self.assertEqual(decoded, plaintext)


class TestNormalizeDevice(unittest.TestCase):
    def test_maps_trir_payload_onto_schema(self) -> None:
        d = TrirConnectLifeApi._normalize_trir_device(_raw_trir_device())
        self.assertEqual(d["deviceFeatureCode"], "109")  # from featureCode
        self.assertEqual(d["bindTime"], 1745691034131)    # from bindDate, as int
        self.assertEqual(d["useTime"], 1745691034087)      # str coerced to int
        self.assertIsNone(d["createTime"])                 # absent -> None
        self.assertIsNone(d["deviceTypeName"])             # absent -> None
        self.assertEqual(d["seq"], 0)                       # absent -> 0

    def test_leaves_eu_shaped_payload_feature_code(self) -> None:
        eu = {
            "deviceTypeCode": "009",
            "deviceFeatureCode": "100",
            "bindTime": 0,
            "useTime": 0,
            "createTime": 0,
            "seq": 5,
        }
        d = TrirConnectLifeApi._normalize_trir_device(eu)
        self.assertEqual(d["deviceFeatureCode"], "100")
        self.assertEqual(d["seq"], 5)


class TestIntOrDefault(unittest.TestCase):
    def test_coercions(self) -> None:
        f = TrirConnectLifeApi._int_or_default
        self.assertEqual(f("123", None), 123)
        self.assertIsNone(f("", None))
        self.assertIsNone(f("null", None))
        self.assertIsNone(f(None, None))
        self.assertIsNone(f("not-a-number", None))
        self.assertEqual(f(None, 0), 0)


class TestTokenState(unittest.TestCase):
    def setUp(self) -> None:
        self.api = TrirConnectLifeApi("user@example.ru", "secret")

    def test_success_sets_tokens_and_expiry(self) -> None:
        before = dt.datetime.now()
        self.api._set_trir_token_state({
            "resultCode": 0,
            "accessToken": "at",
            "refreshToken": "rt",
            "accessTokenExpiredTime": 86400,
            "refreshTokenExpiredTime": 2592000,
        })
        self.assertEqual(self.api._access_token, "at")
        self.assertEqual(self.api._refresh_token, "rt")
        expires = self.api._expires
        assert expires is not None
        # Renewed 90s before expiry.
        self.assertGreater(expires, before + dt.timedelta(seconds=86400 - 200))
        self.assertLess(expires, before + dt.timedelta(seconds=86400))
        self.assertIsNotNone(self.api._refresh_token_expires)

    def test_non_zero_result_code_raises(self) -> None:
        with self.assertRaises(LifeConnectAuthError):
            self.api._set_trir_token_state({"resultCode": 1, "errorCode": 600904})

    def test_missing_access_token_raises(self) -> None:
        with self.assertRaises(LifeConnectAuthError):
            self.api._set_trir_token_state({"resultCode": 0, "refreshToken": "rt"})


# -- Flows (scripted session) ------------------------------------------------

class TestFlows(unittest.IsolatedAsyncioTestCase):
    async def test_login_sets_tokens(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        requests: list[tuple[str, str, Any]] = [
            ("POST", api.login_url, _token_response()),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            self.assertTrue(await api.authenticate())
        self.assertEqual(api._access_token, "access-token")
        self.assertEqual(api._refresh_token, "refresh-token")
        self.assertFalse(requests)

    async def test_get_appliances_json_returns_raw_payload(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        requests: list[tuple[str, str, Any]] = [
            ("POST", api.login_url, _token_response()),
            ("POST", api.gateway_device_list_url, _envelope({"resultCode": 0, "deviceList": [_raw_trir_device()]})),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()
        # The JSON path is raw: featureCode present, no synthetic deviceFeatureCode.
        self.assertEqual(result[0]["featureCode"], "109")
        self.assertNotIn("deviceFeatureCode", result[0])
        self.assertFalse(requests)

    async def test_get_appliances_builds_normalized_appliances(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        requests: list[tuple[str, str, Any]] = [
            ("POST", api.login_url, _token_response()),
            ("POST", api.gateway_device_list_url, _envelope({"resultCode": 0, "deviceList": [_raw_trir_device()]})),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            appliances = await api.get_appliances()
        self.assertEqual(len(appliances), 1)
        self.assertEqual(appliances[0].device_type_code, "009")
        self.assertEqual(appliances[0].device_feature_code, "109")
        self.assertFalse(requests)

    async def test_update_appliance_posts_to_property_set(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        requests: list[tuple[str, str, Any]] = [
            ("POST", api.login_url, _token_response()),
            ("POST", api.gateway_update_url, _envelope({"resultCode": 0})),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api.update_appliance("puid-1", {"t_power": "1"})
        self.assertFalse(requests)

    async def test_reauth_retry_on_invalid_access_token(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        requests: list[tuple[str, str, Any]] = [
            ("POST", api.login_url, _token_response("first")),
            ("POST", api.gateway_device_list_url, _envelope({"resultCode": 1, "errorCode": 100026, "errorDesc": "AccessToken Invalid"})),
            ("POST", api.login_url, _token_response("second")),
            ("POST", api.gateway_device_list_url, _envelope({"resultCode": 0, "deviceList": [_raw_trir_device()]})),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            result = await api.get_appliances_json()
        self.assertEqual(len(result), 1)
        self.assertEqual(api._access_token, "second")
        self.assertFalse(requests)

    async def test_refresh_failure_falls_back_to_login(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        api._access_token = "expired"
        api._refresh_token = "refresh"
        api._expires = dt.datetime.now() - dt.timedelta(seconds=1)
        api._refresh_token_expires = dt.datetime.now() + dt.timedelta(days=1)

        requests: list[tuple[str, str, Any]] = [
            ("POST", api.refresh_url, _envelope({"resultCode": 1, "errorCode": 611702, "errorDesc": "token illegal"})),
            ("POST", api.login_url, _token_response("relogin")),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api._fetch_access_token()
        self.assertEqual(api._access_token, "relogin")
        self.assertFalse(requests)

    async def test_refresh_success_updates_tokens(self) -> None:
        api = TrirConnectLifeApi("user@example.ru", "secret")
        api._access_token = "expired"
        api._refresh_token = "refresh"
        api._expires = dt.datetime.now() - dt.timedelta(seconds=1)
        api._refresh_token_expires = dt.datetime.now() + dt.timedelta(days=1)

        requests: list[tuple[str, str, Any]] = [
            ("POST", api.refresh_url, _token_response("refreshed")),
        ]
        with patch.object(api_module.aiohttp, "ClientSession", new=FakeClientSessionFactory(requests)):
            await api._fetch_access_token()
        self.assertEqual(api._access_token, "refreshed")
        self.assertFalse(requests)


if __name__ == "__main__":
    unittest.main()
