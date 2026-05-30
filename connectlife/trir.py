"""ConnectLife.TRIR API client.

TRIR is the Hisense ConnectLife variant used in Russia/CIS. It runs on the same
HijuConn gateway infrastructure as the EU service (same host family, same vendor
secret, same `/device/pu/property/set` and `/clife-svc/pu/air_duct_energy`
endpoints, same `statusList` schema), but differs in three ways:

1. Auth: a single `/account/acc/login_pwd` POST (AES-CBC "HENC-" transport
   envelope + RSA-encrypted password) instead of the Gigya/OAuth2 chain.
2. Request signing: ``SHA1(app_secret + sorted_items + vendor_secret)`` instead
   of the EU ``RSA(SHA256(sorted_items + suffix))``. TRIR also drops falsy
   fields from the signed string.
3. Device list: ``POST /br/getDeviceTabList`` (the EU
   ``/clife-svc/pu/get_device_status_list`` returns 405 on the RU gateway). The
   per-device payload uses slightly different field names that we normalize back
   onto the EU ``ConnectLifeAppliance`` schema.

Token lifecycle (refresh + re-login fallback) is inherited unchanged from
``ConnectLifeApi._fetch_access_token``: we only override how the initial login
and the refresh are performed. If refresh fails for any reason, the base class
already resets tokens and performs a full re-login, so refresh is effectively
optional — TRIR login is a single cheap request.

Reverse engineering by @vit9696, https://github.com/oyvindwe/connectlife-ha/issues/267.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import secrets
import uuid
from typing import Any, cast

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .api import ConnectLifeApi, LifeConnectAuthError, LifeConnectError

_LOGGER = logging.getLogger(__name__)

TRIR_BASE_URL = "https://clife-ru2-gateway.hijuconn.com"

# Shared with the EU gateway (== GATEWAY_SIGN_SUFFIX in api.py).
TRIR_VENDOR_SECRET = "D9519A4B756946F081B7BB5B5E8D1197"

# Per-platform application identities extracted from the mobile apps.
TRIR_APPS = {
    "android": {
        "app_id": "17930742094888975",
        "app_secret": "uyIr_SJpd6-aL-yTMq50idPbmeL3LXs6vOExhXy_LuhqaIHZGz46nI9BYlWQPel0",
        "client_id": "td0010010000",
    },
    "ios": {
        "app_id": "17930742096986127",
        "app_secret": "j4FmB-xO2bfiTiM7hoVBp8_ADxH49Nb9TVX_UGUx1qMbUwVGN3Nq47hPt5IH3NuK",
        "client_id": "td0010020000",
    },
}

# AES-256-CBC transport obfuscation for the login request body. The key/IV are
# hardcoded in the app; the magic prefix marks an encrypted payload.
TRIR_TRANSPORT_MAGIC = "HENC-"
TRIR_AES_KEY = b"aaaabbbbccccddddeeeeffffgggghhhh"
TRIR_AES_IV = b"aaaabbbbccccdddd"

# RSA public key used to wrap the (MD5-hashed, upper-hex) password.
TRIR_PASSWORD_PUBLIC_KEY = cast(
    RSAPublicKey,
    serialization.load_pem_public_key(
        b"-----BEGIN PUBLIC KEY-----\n"
        b"MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAL1pyw5RThDowxOMDeV/p5vY3f8o5hgt\n"
        b"hurwD9Ybby5OVQl3gyHLPie4j6HVmDCMypWbGt94LvpYtVW3ZDVIAc0CAwEAAQ==\n"
        b"-----END PUBLIC KEY-----\n"
    ),
)

# Fallback token lifetime if the server omits accessTokenExpiredTime.
TRIR_DEFAULT_TOKEN_LIFETIME = 7200
TRIR_TOKEN_RENEW_MARGIN = 90

# Invalid/expired access token; confirmed against the live TRIR gateway.
TRIR_INVALID_ACCESS_TOKEN = 100026
# randStr check failure; inherited from the EU gateway value.
TRIR_RANDSTR_CHECK_FAILED = 101005


class TrirConnectLifeApi(ConnectLifeApi):
    """ConnectLife API client for the Russia/CIS (TRIR) backend."""

    # The captured client identifies as a Flutter/Dart app; mirror it in case
    # the gateway is picky about the User-Agent.
    gateway_user_agent = "Dart/3.8 (dart:io)"
    invalid_access_token_code = TRIR_INVALID_ACCESS_TOKEN
    randstr_check_failed_code = TRIR_RANDSTR_CHECK_FAILED

    def __init__(
        self,
        username: str,
        password: str,
        test_server: str | None = None,
        *,
        device_uuid: str | None = None,
        platform: str = "android",
        language_id: str = "1",
        timezone: str = "Europe/Moscow",
        version: str = "8.1",
    ):
        """Initialize the TRIR client.

        device_uuid should be stable across restarts (the integration ought to
        generate one and persist it in the config entry), because the derived
        ``sourceId`` ties the session to a "device". A fresh UUID each start
        likely still works but registers a new pseudo-device every time.
        """
        super().__init__(username, password)

        app = TRIR_APPS[platform]
        self.app_id = app["app_id"]
        self.app_secret = app["app_secret"]
        self.client_id = app["client_id"]
        self.vendor_secret = TRIR_VENDOR_SECRET

        self.language_id = language_id
        self.timezone = timezone
        self.version = version

        raw_uuid = uuid.UUID(device_uuid) if device_uuid else uuid.uuid4()
        device_id = hashlib.md5(raw_uuid.bytes.hex().encode()).hexdigest()
        self.source_id = f"{self.client_id}{device_id}"

        base = test_server or TRIR_BASE_URL
        self.login_url = f"{base}/account/acc/login_pwd"
        self.refresh_url = f"{base}/account/acc/refresh_token"
        self.gateway_device_list_url = f"{base}/br/getDeviceTabList"
        self.gateway_update_url = f"{base}/device/pu/property/set"
        self.gateway_energy_url = f"{base}/clife-svc/pu/air_duct_energy"

    # -- Auth: initial login -------------------------------------------------

    async def _initial_access_token(self) -> None:
        request_data = self._trir_base_payload()
        request_data["accessToken"] = None
        request_data["loginName"] = self._username
        request_data["password"] = self._encode_password(self._password)
        request_data["sign"] = self._sign_gateway_request(request_data)

        body = json.dumps(request_data, separators=(",", ":"))
        envelope = await self._trir_post(self.login_url, self._encrypt_transport(body))
        self._set_trir_token_state(envelope)

    # -- Auth: refresh (falls back to re-login via base _fetch_access_token) --

    async def _refresh_access_token(self) -> None:
        request_data = self._trir_base_payload()
        # The current (expired) access token is still sent alongside the refresh
        # token, matching the app's behavior.
        request_data["accessToken"] = self._access_token
        request_data["refreshToken"] = self._refresh_token
        request_data["sign"] = self._sign_gateway_request(request_data)

        # The refresh request is plain JSON (not HENC-wrapped).
        body = json.dumps(request_data, separators=(",", ":"))
        envelope = await self._trir_post(self.refresh_url, body)
        self._set_trir_token_state(envelope)

    def _set_trir_token_state(self, response: dict[str, Any]) -> None:
        """Validate a login/refresh reply and store the resulting tokens.

        Raises LifeConnectAuthError on failure so the base class falls back to a
        full re-login.
        """
        result_code = response.get("resultCode")
        if result_code not in (0, "0"):
            error_code = response.get("errorCode")
            error_desc = response.get("errorDesc") or "Unknown TRIR auth error"
            raise LifeConnectAuthError(
                f"TRIR auth failed: code={error_code} description='{error_desc}'"
            )

        self._access_token = self._require_auth_field(response, "accessToken")
        self._refresh_token = response.get("refreshToken", self._refresh_token)

        now = dt.datetime.now()
        # *ExpiredTime fields are lifetimes in seconds (not absolute). Compute
        # from "now" to avoid depending on the server clock. TODO(#267): verify.
        access_lifetime = (
            self._int_or_default(response.get("accessTokenExpiredTime"), None)
            or TRIR_DEFAULT_TOKEN_LIFETIME
        )
        self._expires = now + dt.timedelta(
            seconds=access_lifetime - TRIR_TOKEN_RENEW_MARGIN
        )
        refresh_lifetime = self._int_or_default(response.get("refreshTokenExpiredTime"), 0) or 0
        self._refresh_token_expires = (
            now + dt.timedelta(seconds=refresh_lifetime) if refresh_lifetime else None
        )

    # -- Device list ---------------------------------------------------------

    async def _request_gateway_appliances_json(
        self, *, retry_on_reauth: bool
    ) -> list[dict[str, Any]]:
        gateway_response = await self._request_gateway_json(
            self.gateway_device_list_url,
            payload={"dataType": "0"},
            retry_on_reauth=retry_on_reauth,
            retry_on_randstr=True,
            method="POST",
        )
        device_list = gateway_response.get("deviceList")
        if not isinstance(device_list, list):
            raise LifeConnectError(
                "Unexpected response from TRIR gateway: missing 'deviceList'",
                endpoint=self.gateway_device_list_url,
            )
        # Returned raw (not mapped onto the EU schema) so dumps record the
        # actual TRIR payload; mapping happens in _normalize_appliance_payloads.
        return device_list

    def _normalize_appliance_payloads(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Map TRIR device payloads onto the EU schema before building appliances."""
        payloads = [self._normalize_trir_device(p) for p in payloads]
        return super()._normalize_appliance_payloads(payloads)

    @classmethod
    def _normalize_trir_device(cls, device: dict[str, Any]) -> dict[str, Any]:
        """Map a TRIR getDeviceTabList entry onto the EU appliance schema."""
        d = dict(device)
        # Field renames.
        d.setdefault("deviceFeatureCode", d.get("featureCode", None))
        # Fields TRIR omits that ConnectLifeAppliance reads unconditionally.
        d.setdefault("deviceFeatureName", None)
        d.setdefault("deviceTypeName", None)
        d.setdefault("seq", 0)
        # Timestamps arrive as strings (and bindTime is named bindDate); the
        # appliance model does `value / 1000`, so they must be ints or falsy.
        d["bindTime"] = cls._int_or_default(d.get("bindTime", d.get("bindDate")), None)
        d["useTime"] = cls._int_or_default(d.get("useTime"), None)
        d["createTime"] = cls._int_or_default(d.get("createTime"), None)
        return d

    # -- Gateway request building / signing (overrides EU implementations) ----

    def _gateway_request_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_data = self._trir_base_payload()
        request_data["accessToken"] = self._require_access_token()
        request_data.update(payload)
        request_data["sign"] = self._sign_gateway_request(request_data)
        return request_data

    def _trir_base_payload(self) -> dict[str, Any]:
        return {
            "timeStamp": str(int(dt.datetime.now().timestamp() * 1000)),
            "version": self.version,
            "languageId": self.language_id,
            "timezone": self.timezone,
            "randStr": secrets.token_hex(16),
            "appId": self.app_id,
            "sourceId": self.source_id,
        }

    def _sign_gateway_request(self, payload: dict[str, Any]) -> str:  # type: ignore[override]
        items = []
        for key in sorted(k for k in payload if k != "sign"):
            value = payload[key]
            if not value:  # TRIR drops falsy fields (incl. accessToken=None).
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            items.append(f"{key}={value}")
        sign_str = f"{self.app_secret}{'&'.join(items)}{self.vendor_secret}"
        return hashlib.sha1(sign_str.encode()).hexdigest()

    # -- Crypto helpers ------------------------------------------------------

    def _encode_password(self, password: str) -> str:
        digest = hashlib.md5(password.encode()).hexdigest().upper().encode()
        encrypted = TRIR_PASSWORD_PUBLIC_KEY.encrypt(digest, padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _encrypt_transport(plaintext: str) -> str:
        padder = sym_padding.PKCS7(128).padder()  # AES block size in bits
        padded = padder.update(plaintext.encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(TRIR_AES_KEY), modes.CBC(TRIR_AES_IV)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return f"{TRIR_TRANSPORT_MAGIC}{base64.b64encode(ciphertext).decode()}"

    # -- HTTP ----------------------------------------------------------------

    async def _trir_post(self, url: str, body: str) -> dict[str, Any]:
        """POST a raw (already-serialized/encrypted) body and unwrap the reply.

        TRIR wraps every reply as ``{"response": {...}, "signatureServer": ...}``;
        we return the inner ``response`` object.
        """
        async with self._client_session() as session:
            async with session.post(
                url,
                data=body,
                headers={
                    "User-Agent": self.gateway_user_agent,
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept-Encoding": "gzip",
                },
            ) as response:
                if response.status != 200:
                    text = await self._read_response_body(response)
                    raise self._response_error(
                        "Unexpected response from TRIR endpoint: status={status}",
                        response,
                        text,
                        endpoint=url,
                        auth=True,
                    )
                envelope = await self._json(response, endpoint=url, auth=True)

        inner = envelope.get("response") if isinstance(envelope, dict) else None
        if not isinstance(inner, dict):
            raise LifeConnectAuthError(
                "Unexpected response from TRIR endpoint: missing 'response'",
                endpoint=url,
            )
        return inner

    # -- Misc ----------------------------------------------------------------

    @staticmethod
    def _int_or_default(value: Any, default: int | None) -> int | None:
        if value in (None, "", "null"):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
