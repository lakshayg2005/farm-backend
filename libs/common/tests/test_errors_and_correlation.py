import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from farm_common.app import create_app
from farm_common.errors import Conflict
from farm_common.log import JsonFormatter, get_correlation_id
from farm_common.settings import ServiceSettings


class Item(BaseModel):
    qty: int


@pytest.fixture
def client() -> TestClient:
    app = create_app("demo", "0.0.1", ServiceSettings(), configure_logs=False)

    @app.post("/stock")
    async def reserve(item: Item) -> dict:
        raise Conflict("Only 3 kg left", code="STOCK_UNAVAILABLE")

    @app.get("/crash")
    async def crash() -> dict:
        raise RuntimeError("boom")

    @app.get("/whoami")
    async def whoami() -> dict:
        return {"correlationId": get_correlation_id()}

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_uses_standard_shape_with_correlation_id(client):
    response = client.post("/stock", json={"qty": 5})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "STOCK_UNAVAILABLE"
    assert error["message"] == "Only 3 kg left"
    assert error["correlationId"] == response.headers["X-Correlation-ID"]


def test_incoming_correlation_id_is_kept(client):
    response = client.get("/whoami", headers={"X-Correlation-ID": "abc123"})

    assert response.headers["X-Correlation-ID"] == "abc123"
    assert response.json() == {"correlationId": "abc123"}


def test_correlation_id_generated_when_missing(client):
    response = client.get("/whoami")

    assert len(response.headers["X-Correlation-ID"]) == 32
    assert response.json()["correlationId"] == response.headers["X-Correlation-ID"]


def test_validation_error_lists_fields(client):
    response = client.post("/stock", json={"qty": "lots"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["field"] == "body.qty"


def test_unknown_route_is_not_found(client):
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_crash_returns_500_without_leaking_details(client):
    response = client.get("/crash")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "Something went wrong",
        "correlationId": response.headers["X-Correlation-ID"],
    }


def test_json_log_line_has_service_name():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("farm",), None)

    line = JsonFormatter("catalog").format(record)

    assert '"service": "catalog"' in line
    assert '"message": "hello farm"' in line
