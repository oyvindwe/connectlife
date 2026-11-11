"""Tests for push updates over a WebSocket, against the test server."""

from __future__ import annotations

import asyncio
import base64
import json
import unittest

from aiohttp.test_utils import TestServer

from connectlife import test_server
from connectlife.api import ConnectLifeApi, LifeConnectError
from connectlife.oauth import OAuthConnectLifeApi
from connectlife.push import ConnectLifePush, PushConnected, PushOnline, PushStatus, parse_message


def _encode(message: dict) -> str:
    return base64.b64encode(json.dumps(message).encode()).decode()


class ParseMessageTest(unittest.TestCase):
    def test_status(self) -> None:
        raw = _encode({
            "msgTypeCode": "status_devicestatus",
            "content": json.dumps({"puid": "pu1", "status": "9PUBQEkB", "properties": {"Door_status": 2}}),
        })
        self.assertEqual(parse_message(raw), PushStatus("pu1", {"Door_status": "2"}))

    def test_state_change_online(self) -> None:
        raw = _encode({
            "msgTypeCode": "iot_devicestate_change",
            "content": {"data": {"deviceId": "pu1", "online": "0", "states": {"workMode.cl": "1"}}},
        })
        self.assertEqual(parse_message(raw), PushOnline("pu1", False))

    def test_wifi_status(self) -> None:
        raw = _encode({"msgTypeCode": "status_wifistatus", "content": json.dumps({"puid": "pu1", "onlinestats": 1})})
        self.assertEqual(parse_message(raw), PushOnline("pu1", True))

    def test_ignored(self) -> None:
        self.assertIsNone(parse_message(_encode({"msgTypeCode": "other", "content": "{}"})))
        self.assertIsNone(parse_message("not base64 json"))
        self.assertIsNone(parse_message(_encode({"msgTypeCode": "status_devicestatus", "content": "{}"})))


class PushTestServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        test_server.appliances.clear()
        test_server.appliances["puid1"] = {
            "deviceId": "1", "puid": "puid1", "deviceNickName": "Washer", "deviceTypeCode": "007",
            "deviceFeatureCode": "400", "statusList": {"t_power": "0"},
        }
        self.server = TestServer(test_server.create_app())
        await self.server.start_server()
        self.api = ConnectLifeApi("user", "password", test_server=str(self.server.make_url("")).rstrip("/"))
        await self.api.login()

    async def asyncTearDown(self) -> None:
        await self.server.close()
        test_server.appliances.clear()

    async def test_pushes_status_after_update(self) -> None:
        push = ConnectLifePush(self.api, "phone")
        events = push.listen()
        self.assertEqual(await anext(events), PushConnected())
        await self.api.update_appliance("puid1", {"t_power": "1"})
        event = await asyncio.wait_for(anext(events), timeout=5)
        self.assertEqual(event, PushStatus("puid1", {"t_power": "1"}))
        await push.close()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), timeout=5)

    async def test_server_closing_ends_listen(self) -> None:
        test_server.push_close_after = 0.1
        try:
            events = [event async for event in ConnectLifePush(self.api, "phone").listen()]
        finally:
            test_server.push_close_after = 0
        self.assertEqual(events, [PushConnected()])

    async def test_offline_flaps(self) -> None:
        test_server.push_offline_every = 0.1
        try:
            push = ConnectLifePush(self.api, "phone")
            events = push.listen()
            self.assertEqual(await anext(events), PushConnected())
            self.assertEqual(await asyncio.wait_for(anext(events), timeout=5), PushOnline("puid1", False))
            self.assertEqual(test_server.appliances["puid1"]["offlineState"], 0)
            self.assertEqual(await asyncio.wait_for(anext(events), timeout=5), PushOnline("puid1", True))
            self.assertEqual(test_server.appliances["puid1"]["offlineState"], 1)
            await push.close()
        finally:
            test_server.push_offline_every = 0

    async def test_oauth_client(self) -> None:
        async def access_token() -> str:
            return "my_access_token"

        api = OAuthConnectLifeApi(access_token, test_server=str(self.server.make_url("")).rstrip("/"))
        push = ConnectLifePush(api, "phone")
        events = push.listen()
        self.assertEqual(await anext(events), PushConnected())
        await push.close()

    async def test_refused_connection_hides_token(self) -> None:
        self.api._access_token = "secret-token"
        with self.assertRaises(LifeConnectError) as raised:
            await anext(ConnectLifePush(self.api, "phone").listen())
        self.assertIn("status 403", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    async def test_close_before_connected(self) -> None:
        push = ConnectLifePush(self.api, "phone")
        register = push.register

        async def register_then_close() -> None:
            await register()
            await push.close()

        push.register = register_then_close  # type: ignore[method-assign]
        self.assertEqual([event async for event in push.listen()], [])


if __name__ == "__main__":
    unittest.main()
