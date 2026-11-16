import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from farm_common.auth import CurrentUser, Role, get_current_user
from notification_service import stubs
from notification_service.main import app

ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = ROOT / "contracts" / "openapi" / "notification.json"


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(stubs.SAMPLE_USER_ID), roles=[Role.CUSTOMER]
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_committed_openapi_spec_is_current():
    """contracts/openapi/notification.json must match the app."""
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert spec == json.loads(json.dumps(app.openapi()))


def test_list_notifications(client):
    response = client.get("/notifications")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "unreadCount" in data
    assert len(data["items"]) >= 1
    assert data["items"][0]["type"] in ["ORDER_PLACED", "PAYMENT_SUCCEEDED"]


def test_list_notifications_unread_only(client):
    response = client.get("/notifications?unreadOnly=true")
    assert response.status_code == 200
    data = response.json()
    assert all(item["readAt"] is None for item in data["items"])


def test_get_unread_count(client):
    response = client.get("/notifications/unread-count")
    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert isinstance(data["count"], int)


def test_register_device_token(client):
    body = {
        "token": "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]",
        "platform": "android",
    }
    response = client.post("/notifications/devices", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["token"] == body["token"]
    assert data["platform"] == "android"
    assert "id" in data


def test_unregister_device_token(client):
    token = "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"
    response = client.delete(f"/notifications/devices/{token}")
    assert response.status_code == 204


def test_mark_notification_read(client):
    notif_id = str(stubs.NOTIFICATION_ID_1)
    response = client.post(f"/notifications/{notif_id}/read")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == notif_id
    assert data["readAt"] is not None


def test_mark_all_read(client):
    response = client.post("/notifications/read-all")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
