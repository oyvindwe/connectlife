"""Push updates from the ConnectLife cloud over a WebSocket.

Experimental. The cloud pushes a device's full status whenever it changes, within seconds, so a client can
update at once instead of waiting for the next poll.

Flow: register a "phone" (a client id the caller keeps between runs, so the account doesn't collect a new one on
every start), fetch the push server and channel, then connect to
``wss://<server>:<ssl port>/ws/<channel>?phoneCode=<code>&token=<access token>``. Messages are base64-encoded JSON
with a ``msgTypeCode`` and a ``content`` JSON string:

- ``status_devicestatus``: ``content.properties`` holds the device's full status (same names and string values as
  ``statusList``); ``content.status`` is the same as a raw device frame. Often repeated unchanged.
- ``iot_devicestate_change``: a few properties under other names, and ``data.online``.
- ``status_wifistatus``: ``onlinestats`` 1 when the device is online.

Usage::

    push = ConnectLifePush(api, phone_code)
    async for event in push.listen():
        if isinstance(event, PushConnected):
            ...  # fetch a snapshot now: pushes from here on are newer
        elif isinstance(event, PushStatus):
            ...  # event.puid, event.properties

``listen()`` ends (or raises) when the connection is lost; reconnecting, with a delay, is up to the caller.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp

from .api import ConnectLifeApi, LifeConnectError

_LOGGER = logging.getLogger(__name__)

STATUS_MESSAGE = "status_devicestatus"
STATE_CHANGE_MESSAGE = "iot_devicestate_change"
WIFI_STATUS_MESSAGE = "status_wifistatus"
DEFAULT_HEARTBEAT = 30


@dataclass(frozen=True)
class PushServer:
    """Where to connect, from the push channel query."""

    host: str
    port: int
    ssl_port: int
    channel: str
    heartbeat: int
    fail_times: int | None


@dataclass(frozen=True)
class PushConnected:
    """The WebSocket is connected: pushes from here on are newer than a snapshot fetched now."""


@dataclass(frozen=True)
class PushStatus:
    """A device's full status."""

    puid: str
    properties: dict[str, str]


@dataclass(frozen=True)
class PushOnline:
    """A device went online or offline."""

    puid: str
    online: bool


PushEvent = PushConnected | PushStatus | PushOnline


def parse_message(raw: str) -> PushStatus | PushOnline | None:
    """Parse one WebSocket text message; None for message types we don't use or can't read."""
    try:
        message = json.loads(base64.b64decode(raw).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _LOGGER.debug("Undecodable push message: %.200r", raw)
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return None
    if not isinstance(content, dict):
        return None

    msg_type = message.get("msgTypeCode")
    if msg_type == STATUS_MESSAGE:
        puid, properties = content.get("puid"), content.get("properties")
        if isinstance(puid, str) and isinstance(properties, dict) and properties:
            return PushStatus(puid, {str(k): str(v) for k, v in properties.items()})
    elif msg_type == STATE_CHANGE_MESSAGE:
        data = content.get("data")
        if isinstance(data, dict) and isinstance(data.get("deviceId"), str) and "online" in data:
            return PushOnline(data["deviceId"], str(data["online"]) == "1")
    elif msg_type == WIFI_STATUS_MESSAGE:
        puid = content.get("puid")
        if isinstance(puid, str) and "onlinestats" in content:
            return PushOnline(puid, str(content["onlinestats"]) == "1")
    else:
        _LOGGER.debug("Ignored push message type %s", msg_type)
    return None


class ConnectLifePush:
    """A push connection for the account an API client is logged in to."""

    def __init__(self, api: ConnectLifeApi, phone_code: str) -> None:
        self._api = api
        self._phone_code = phone_code
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def register(self) -> None:
        """Register the phone code for push messages."""
        await self._api._fetch_access_token()
        await self._api._request_gateway_json(
            self._api.gateway_push_register_url,
            payload={"phoneCode": self._phone_code},
            retry_on_reauth=True,
        )

    async def get_server(self) -> PushServer:
        """Fetch the push server and channel for the phone code."""
        await self._api._fetch_access_token()
        info = await self._api._request_gateway_json(
            self._api.gateway_push_channels_url,
            payload={"pageNo": "1", "pageSize": "1", "phoneCode": self._phone_code, "queryType": 2},
            retry_on_reauth=True,
            method="GET",
        )
        channels = info.get("pushChannels") or []
        if not channels or not info.get("pushServerIp"):
            raise LifeConnectError("No push channel in response", endpoint=self._api.gateway_push_channels_url)
        return PushServer(
            host=info["pushServerIp"],
            port=int(info.get("pushServerPort") or 0),
            ssl_port=int(info.get("pushServerSslPort") or 0),
            channel=channels[0]["pushChannel"],
            heartbeat=int(info.get("hbInterval") or DEFAULT_HEARTBEAT),
            fail_times=int(info["hbFailTimes"]) if info.get("hbFailTimes") else None,
        )

    def _url(self, server: PushServer) -> str:
        port = server.ssl_port if self._api.push_scheme == "wss" else server.port
        return f"{self._api.push_scheme}://{server.host}:{port}/ws/{server.channel}"

    async def listen(self) -> AsyncIterator[PushEvent]:
        """Register, connect and yield events until the connection is lost or close() is called."""
        await self.register()
        server = await self.get_server()
        token = self._api._require_access_token()
        url = self._url(server)
        _LOGGER.debug("Connecting to push server %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url,
                params={"phoneCode": self._phone_code, "token": token},
                heartbeat=server.heartbeat,
            ) as ws:
                self._ws = ws
                try:
                    yield PushConnected()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if (event := parse_message(msg.data)) is not None:
                                yield event
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise LifeConnectError(f"Push connection failed: {ws.exception()}", endpoint=url)
                finally:
                    self._ws = None
        _LOGGER.debug("Push connection closed")

    async def close(self) -> None:
        """Close the connection, ending listen()."""
        if self._ws is not None:
            await self._ws.close()
