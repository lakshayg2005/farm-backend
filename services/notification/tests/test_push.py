"""Tests for the Expo/FCM push notification client."""

from unittest.mock import AsyncMock, patch

import anyio
import httpx

from notification_service.push import send_push_notification


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
