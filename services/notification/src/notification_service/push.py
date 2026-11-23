"""Push delivery: FCM (HTTP v1) for Android device tokens, the Expo push API for Expo tokens.

The sender is picked by token shape: `ExponentPushToken[...]` goes to Expo, everything else is a
raw FCM registration token. FCM needs an OAuth access token minted from a Firebase service account
(`FCM_SERVICE_ACCOUNT_JSON`); the token is cached until shortly before it expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REFRESH_MARGIN_S = 120


def is_expo_token(token: str) -> bool:
    return token.startswith(("ExponentPushToken[", "ExpoPushToken["))


def stringify_data(data: dict[str, Any] | None, **extra: str) -> dict[str, str]:
    """FCM data values must all be strings: drop nulls, JSON-encode anything that is not text."""
    out: dict[str, str] = {}
    for key, value in {**(data or {}), **extra}.items():
        if value is None:
            continue
        out[key] = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return out


class FcmClient:
    """Sends messages through the FCM HTTP v1 API with a service-account OAuth token."""

    def __init__(self, service_account_json: str, project_id: str | None = None) -> None:
        info = json.loads(service_account_json)
        self._client_email: str = info["client_email"]
        self._private_key: str = info["private_key"]
        self._token_uri: str = info.get("token_uri") or DEFAULT_TOKEN_URI
        self.project_id: str = project_id or info["project_id"]
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _token(self, client: httpx.AsyncClient) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - _REFRESH_MARGIN_S:
                return self._access_token
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self._client_email,
                    "scope": FCM_SCOPE,
                    "aud": self._token_uri,
                    "iat": now,
                    "exp": now + 3600,
                },
                self._private_key,
                algorithm="RS256",
            )
            resp = await client.post(
                self._token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._access_token = body["access_token"]
            self._expires_at = time.time() + float(body.get("expires_in", 3600))
            return self._access_token  # type: ignore[return-value]

    async def send(
        self,
        client: httpx.AsyncClient,
        token: str,
        title: str,
        body: str,
        data: dict[str, str],
    ) -> str:
        """Send one message. Returns "ok", "invalid" (token is dead, delete it) or "error"."""
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": data,
                "android": {
                    "priority": "HIGH",
                    "notification": {"sound": "default"},
                },
            }
        }
        try:
            access = await self._token(client)
            resp = await client.post(
                FCM_SEND_URL.format(project_id=self.project_id),
                json=message,
                headers={"Authorization": f"Bearer {access}"},
            )
        except Exception as exc:
            logger.warning("FCM send failed: %s", exc)
            return "error"
        if resp.status_code < 300:
            return "ok"
        if resp.status_code == 401:
            self._access_token = None  # expired or revoked: mint a new one next time
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        codes = {d.get("errorCode") for d in err.get("details", []) if isinstance(d, dict)}
        if resp.status_code == 404 or "UNREGISTERED" in codes:
            return "invalid"
        if resp.status_code == 400 and "INVALID_ARGUMENT" in (err.get("status"), *codes):
            return "invalid"
        logger.warning("FCM returned %s: %s", resp.status_code, resp.text[:300])
        return "error"


async def send_expo_push(
    client: httpx.AsyncClient,
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any],
    url: str,
    access_token: str | None,
) -> bool:
    messages = [
        {
            "to": t,
            "sound": "default",
            "title": title,
            "body": body,
            "data": data,
            "priority": "high",
        }
        for t in tokens
    ]
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        resp = await client.post(url, json=messages, headers=headers)
    except Exception as exc:
        logger.warning("Failed to send push notification: %s", exc)
        return False
    if resp.status_code >= 400:
        logger.warning("Expo push API returned status %s: %s", resp.status_code, resp.text)
        return False
    logger.info("Sent push notification to %d Expo device(s)", len(tokens))
    return True


async def send_push_notification(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    expo_push_url: str = "https://exp.host/--/api/v2/push/send",
    access_token: str | None = None,
    fcm: FcmClient | None = None,
    notification_type: str | None = None,
    invalid_tokens: list[str] | None = None,
) -> bool:
    """Push to every token, picking Expo or FCM by token shape.

    Tokens FCM reports as dead are appended to `invalid_tokens` so the caller can delete them.
    Returns False if any send failed.
    """
    if not tokens:
        return True
    expo = [t for t in tokens if is_expo_token(t)]
    native = [t for t in tokens if not is_expo_token(t)]
    extra = {"type": notification_type} if notification_type else {}
    str_data = stringify_data(data, title=title, body=body, **extra)

    ok = True
    async with httpx.AsyncClient(timeout=10.0) as client:
        jobs = []
        if expo:
            jobs.append(
                send_expo_push(
                    client,
                    expo,
                    title,
                    body,
                    {**(data or {}), **str_data},
                    expo_push_url,
                    access_token,
                )
            )
        if native:
            if fcm is None:
                logger.warning(
                    "No FCM credentials configured; %d native device(s) not pushed", len(native)
                )
                ok = False
            else:
                results = await asyncio.gather(
                    *(fcm.send(client, t, title, body, str_data) for t in native)
                )
                for token, result in zip(native, results, strict=True):
                    if result == "invalid" and invalid_tokens is not None:
                        invalid_tokens.append(token)
                    ok = ok and result == "ok"
        if jobs:
            ok = all(await asyncio.gather(*jobs)) and ok
    return ok
