"""Tests for the OAuth client (third-party API with caller-managed tokens)."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from connectlife import api as api_module
from connectlife import test_server
from connectlife.api import (
    GATEWAY_ENERGY_CONSUMPTION_URL,
    GATEWAY_SYSTEM_DATA_URL,
    GATEWAY_INVALID_ACCESS_TOKEN,
    LifeConnectAuthError,
)
from connectlife.oauth import (
    JUAPI_BASE_URL,
    OAUTH2_CLIENT_ID,
    OAuthConnectLifeApi,
    generate_source_id,
)

DEVICE_LIST_URL = f"{JUAPI_BASE_URL}/clife-svc/pu/get_device_status_list"
UPDATE_URL = f"{JUAPI_BASE_URL}/device/pu/property/set"


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def json(self) -> Any:
        return self._payload


class RecordingSession:
    """aiohttp ClientSession stand-in: scripted replies, records each request."""

    def __init__(self, replies: list[tuple[str, Any]], requests: list[dict[str, Any]]) -> None:
        self._replies = replies
        self._requests = requests

    async def __aenter__(self) -> "RecordingSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._reply("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._reply("POST", url, kwargs)

    def _reply(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self._requests.append({"method": method, "url": url, **kwargs})
        expected_url, payload = self._replies.pop(0)
        if not url.startswith(expected_url):
            raise AssertionError(f"Expected {expected_url}, got {method} {url}")
        return FakeResponse(payload)


def _patched_session(replies: list[tuple[str, Any]], requests: list[dict[str, Any]]):
    return patch.object(
        api_module.aiohttp, "ClientSession", new=lambda *a, **k: RecordingSession(replies, requests)
    )


def _token_source(*tokens: str):
    """An access token callback returning ``tokens`` in turn, counting the calls."""
    calls = []

    async def access_token() -> str:
        calls.append(None)
        return tokens[min(len(calls), len(tokens)) - 1]

    return access_token, calls


def _appliance() -> dict[str, Any]:
    return {
        "wifiId": "wifi-1", "deviceId": "device-1", "puid": "puid-1",
        "deviceNickName": "Dishwasher", "deviceFeatureCode": "dishwasher-60.2",
        "deviceFeatureName": "Dishwasher", "deviceTypeCode": "015", "deviceTypeName": "",
        "role": 1, "roomId": 1, "roomName": "Kitchen", "offlineState": 1, "seq": 1,
        "bindTime": 0, "useTime": 0, "createTime": 0, "statusList": {"Child_lock": "0"},
    }


class _SignedRequest:
    """Minimal request for the test server's signature check."""

    def __init__(self, method: str, url: str, headers: dict[str, str]) -> None:
        self.method = method
        self.path_qs = url.removeprefix(JUAPI_BASE_URL)
        self.headers = headers


class TestOAuthRequests(unittest.IsolatedAsyncioTestCase):
    async def test_get_appliances_uses_signed_get_with_token_header(self) -> None:
        access_token, calls = _token_source("token-1")
        api = OAuthConnectLifeApi(access_token, source_id="source-1")
        requests: list[dict[str, Any]] = []
        replies = [(DEVICE_LIST_URL, {"resultCode": 0, "deviceList": [_appliance()]})]

        with _patched_session(replies, requests):
            appliances = await api.get_appliances()

        self.assertEqual([a.device_id for a in appliances], ["device-1"])
        self.assertEqual(len(calls), 1)
        (request,) = requests
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["headers"]["accessToken"], "token-1")
        self.assertIn("sourceId=source-1", request["url"])
        self.assertNotIn("accessToken=", request["url"])
        self.assertEqual(request["headers"]["hi-params-encrypt"], OAUTH2_CLIENT_ID)
        self.assertTrue(
            test_server._juapi_signature_valid(
                _SignedRequest("GET", request["url"], request["headers"])
            )
        )

    async def test_update_appliance_signs_body_with_token(self) -> None:
        access_token, _ = _token_source("token-1")
        api = OAuthConnectLifeApi(access_token)
        requests: list[dict[str, Any]] = []
        replies = [(UPDATE_URL, {"resultCode": 0})]

        with _patched_session(replies, requests):
            await api.update_appliance("puid-1", {"Child_lock": "1"})

        (request,) = requests
        body = json.loads(request["data"])
        self.assertEqual(body["accessToken"], "token-1")
        self.assertEqual(body["properties"], {"Child_lock": "1"})
        self.assertEqual(body["appId"], OAUTH2_CLIENT_ID)
        digest = base64.b64encode(hashlib.sha256(request["data"].encode()).digest()).decode()
        self.assertEqual(request["headers"]["Digest"], f"SHA-256={digest}")

    async def test_energy_goes_to_the_app_gateway(self) -> None:
        access_token, _ = _token_source("token-1")
        api = OAuthConnectLifeApi(access_token)
        requests: list[dict[str, Any]] = []
        replies = [(GATEWAY_ENERGY_CONSUMPTION_URL, {"response": {"resultCode": 0, "resultData": {
            "electricCurve": {}, "waterCurve": {},
        }}})]

        with _patched_session(replies, requests):
            result = await api.get_energy_consumption_curve("puid-1", "015", "dishwasher-60.2")

        self.assertIsNotNone(result)
        (request,) = requests
        self.assertNotIn("hi-params-encrypt", request.get("headers", {}))

    def test_routing_of_device_probes(self) -> None:
        access_token, _ = _token_source("token-1")
        api = OAuthConnectLifeApi(access_token)
        # Seen on the third-party API.
        self.assertEqual(api.gateway_self_check_url, f"{JUAPI_BASE_URL}/basic/self_check/info")
        self.assertEqual(api.gateway_hour_power_url, f"{JUAPI_BASE_URL}/clife-svc/pu/get_hour_power")
        # Only seen on the app gateway.
        self.assertEqual(api.gateway_system_data_url, GATEWAY_SYSTEM_DATA_URL)
        self.assertIn(api.gateway_system_data_url, api._app_gateway_urls)

    async def test_rejected_token_is_fetched_again_once(self) -> None:
        access_token, calls = _token_source("stale", "fresh")
        api = OAuthConnectLifeApi(access_token)
        requests: list[dict[str, Any]] = []
        rejected = {"resultCode": 1, "errorCode": GATEWAY_INVALID_ACCESS_TOKEN, "errorDesc": "invalid"}
        replies = [
            (DEVICE_LIST_URL, rejected),
            (DEVICE_LIST_URL, {"resultCode": 0, "deviceList": []}),
        ]

        with _patched_session(replies, requests):
            await api.get_appliances_json()

        self.assertEqual(len(calls), 2)
        self.assertEqual([r["headers"]["accessToken"] for r in requests], ["stale", "fresh"])

    async def test_token_rejected_twice_raises_auth_error(self) -> None:
        access_token, _ = _token_source("stale")
        api = OAuthConnectLifeApi(access_token)
        rejected = {"resultCode": 1, "errorCode": GATEWAY_INVALID_ACCESS_TOKEN, "errorDesc": "invalid"}
        replies = [(DEVICE_LIST_URL, rejected), (DEVICE_LIST_URL, rejected)]

        with _patched_session(replies, []), self.assertRaises(LifeConnectAuthError):
            await api.get_appliances_json()

    def test_generate_source_id(self) -> None:
        source_id = generate_source_id()
        self.assertTrue(source_id.startswith("td001002000"))
        self.assertEqual(len(source_id), 43)


class TestTestServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        test_server.appliances["puid-1"] = _appliance()
        app = web.Application()
        app.add_routes([
            web.get("/login", test_server.oauth_login),
            web.get("/clife-svc/pu/get_device_status_list", test_server.juapi(test_server.get_device_status_list)),
        ])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        test_server.appliances.clear()

    async def test_login_redirects_with_code(self) -> None:
        response = await self.client.get(
            "/login",
            params={"redirect_uri": "https://example.com/cb", "state": "s1"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(
            response.headers["Location"], "https://example.com/cb?code=my_authorization_token&state=s1"
        )

    async def test_oauth_client_reads_flat_device_list(self) -> None:
        access_token, _ = _token_source("token-1")
        api = OAuthConnectLifeApi(access_token, str(self.client.make_url("")).rstrip("/"))

        appliances = await api.get_appliances()

        self.assertEqual([a.device_id for a in appliances], ["device-1"])

    async def test_unsigned_request_gets_the_app_gateway_envelope(self) -> None:
        response = await self.client.get("/clife-svc/pu/get_device_status_list")
        body = await response.json()
        self.assertIn("response", body)

    async def test_bad_signature_is_rejected(self) -> None:
        response = await self.client.get(
            "/clife-svc/pu/get_device_status_list",
            headers={"hi-params-encrypt": OAUTH2_CLIENT_ID, "Authorization": 'Signature signature="bad"'},
        )
        body = await response.json()
        self.assertEqual(body["errorCode"], 600912)
