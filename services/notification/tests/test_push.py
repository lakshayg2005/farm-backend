"""Tests for the Expo/FCM push notification client."""

import json
from unittest.mock import AsyncMock, patch

import anyio
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from notification_service.push import (
    FcmClient,
    is_expo_token,
    send_push_notification,
    stringify_data,
)


def test_send_push_notification_empty_tokens():
    anyio.run(send_push_notification, [], "Title", "Body")


def test_send_push_notification_success():
    async def _test():
        mock_resp = AsyncMock(status_code=200)
        with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
            result = await send_push_notification(
                tokens=["ExponentPushToken[123456]"],
                title="Order Placed",
                body="Your order is confirmed",
                data={"orderId": "123"},
            )
            assert result is True
            mock_post.assert_awaited_once()
            args, kwargs = mock_post.call_args
            assert kwargs["json"][0]["to"] == "ExponentPushToken[123456]"
            assert kwargs["json"][0]["title"] == "Order Placed"

    anyio.run(_test)


def test_send_push_notification_failure_handled():
    async def _test():
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Network down")):
            result = await send_push_notification(
                tokens=["ExponentPushToken[123456]"],
                title="Order Placed",
                body="Your order is confirmed",
            )
            assert result is False

    anyio.run(_test)


# --- FCM -------------------------------------------------------------------------------------


def _service_account() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "project_id": "demo-project",
            "client_email": "sa@demo-project.iam.gserviceaccount.com",
            "private_key": pem,
            "token_uri": "https://oauth.test/token",
        }
    )


def _fake_google(fcm_status: int = 200, fcm_body: dict | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth.test":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(fcm_status, json=fcm_body or {"name": "projects/x/messages/1"})

    return calls, handler


def test_token_shape_and_data_are_stringified():
    assert is_expo_token("ExponentPushToken[abc]")
    assert not is_expo_token("fcm-registration-token:APA91b")
    assert stringify_data({"orderId": "o1", "total": 5.5, "none": None}, type="ORDER_PACKED") == {
        "orderId": "o1",
        "total": "5.5",
        "type": "ORDER_PACKED",
    }


def test_fcm_sends_high_priority_with_ids_in_data():
    async def _test():
        calls, handler = _fake_google()
        sent: list[str] = []
        real = httpx.AsyncClient

        def factory(*a, **kw):
            return real(*a, transport=httpx.MockTransport(handler), **kw)

        with patch("notification_service.push.httpx.AsyncClient", factory):
            ok = await send_push_notification(
                tokens=["fcm-token-1"],
                title="Order Packed",
                body="Packed",
                data={"orderId": "o-1"},
                fcm=FcmClient(_service_account()),
                notification_type="ORDER_PACKED",
            )
        assert ok is True
        token_call, send_call = calls
        assert b"jwt-bearer" in token_call.content
        assert str(send_call.url) == (
            "https://fcm.googleapis.com/v1/projects/demo-project/messages:send"
        )
        assert send_call.headers["authorization"] == "Bearer tok"
        msg = json.loads(send_call.content)["message"]
        assert msg["token"] == "fcm-token-1"
        assert msg["android"]["priority"] == "HIGH"
        assert msg["data"]["orderId"] == "o-1"
        assert msg["data"]["type"] == "ORDER_PACKED"
        assert msg["data"]["title"] == "Order Packed"
        assert sent == []

    anyio.run(_test)


def test_fcm_reports_unregistered_token_and_caches_access_token():
    async def _test():
        calls, handler = _fake_google(
            404,
            {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}},
        )
        real = httpx.AsyncClient

        def factory(*a, **kw):
            return real(*a, transport=httpx.MockTransport(handler), **kw)

        fcm = FcmClient(_service_account())
        dead: list[str] = []
        with patch("notification_service.push.httpx.AsyncClient", factory):
            for _ in range(2):
                ok = await send_push_notification(["gone"], "t", "b", fcm=fcm, invalid_tokens=dead)
                assert ok is False
        assert dead == ["gone", "gone"]
        assert sum(c.url.host == "oauth.test" for c in calls) == 1  # token minted once

    anyio.run(_test)


def test_expo_and_fcm_tokens_are_routed_separately():
    async def _test():
        calls, handler = _fake_google()
        real = httpx.AsyncClient

        def factory(*a, **kw):
            return real(*a, transport=httpx.MockTransport(handler), **kw)

        with patch("notification_service.push.httpx.AsyncClient", factory):
            await send_push_notification(
                ["ExponentPushToken[x]", "native"], "t", "b", fcm=FcmClient(_service_account())
            )
        hosts = [c.url.host for c in calls]
        assert "exp.host" in hosts and "fcm.googleapis.com" in hosts

    anyio.run(_test)


def test_native_token_without_credentials_is_not_sent():
    async def _test():
        calls, handler = _fake_google()
        real = httpx.AsyncClient

        def factory(*a, **kw):
            return real(*a, transport=httpx.MockTransport(handler), **kw)

        with patch("notification_service.push.httpx.AsyncClient", factory):
            assert await send_push_notification(["native"], "t", "b") is False
        assert calls == []

    anyio.run(_test)
