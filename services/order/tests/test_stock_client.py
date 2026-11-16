import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import pytest

from farm_common.errors import AppError
from order_service.stock import HttpStockClient, StockRequestItem

from .conftest import FARM_A, FARMER_A, TOMATO

# (method, path) -> (status, reply)
REPLIES: dict[tuple[str, str], tuple[int, object]] = {}
RECEIVED: list[tuple[str, str, dict | None]] = []


class Handler(BaseHTTPRequestHandler):
    def _reply(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        RECEIVED.append((method, self.path, body))
        status, reply = REPLIES[(method, self.path)]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if reply is not None:
            self.wfile.write(json.dumps(reply).encode())

    def do_GET(self):
        self._reply("GET")

    def do_POST(self):
        self._reply("POST")

    def log_message(self, *args):
        pass


@pytest.fixture
def catalog_url():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    REPLIES.clear()
    RECEIVED.clear()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


ITEMS = [StockRequestItem(TOMATO, 2)]
RESERVE = ("POST", "/internal/stock/reserve")


async def test_farms_of_reads_the_public_listing_route(catalog_url):
    other = uuid4()
    REPLIES[("GET", f"/listings/{TOMATO}")] = (200, {"id": str(TOMATO), "farmId": str(FARM_A)})
    REPLIES[("GET", f"/listings/{other}")] = (200, {"id": str(other), "farmId": str(FARM_A)})

    farms = await HttpStockClient(catalog_url).farms_of([TOMATO, other])

    assert farms == {TOMATO: FARM_A, other: FARM_A}


async def test_farms_of_maps_404_to_listing_not_found(catalog_url):
    REPLIES[("GET", f"/listings/{TOMATO}")] = (404, {"error": {"code": "LISTING_NOT_FOUND"}})

    with pytest.raises(AppError) as error:
        await HttpStockClient(catalog_url).farms_of([TOMATO])

    assert (error.value.code, error.value.status_code) == ("LISTING_NOT_FOUND", 404)
    assert error.value.details == [{"listingId": str(TOMATO)}]


async def test_reserve_sends_catalogs_request_and_parses_its_reply(catalog_url):
    order_id = uuid4()
    REPLIES[RESERVE] = (
        200,
        {
            "orderId": str(order_id),
            "items": [
                {
                    "listingId": str(TOMATO),
                    "farmId": str(FARM_A),
                    "farmerId": str(FARMER_A),
                    "cropId": "tomato",
                    "cropName": "Tomato",
                    "variety": "Roma",
                    "unit": "kg",
                    "qty": 2,
                    "unitPrice": 40.5,
                    "grade": None,
                    "pickupLocation": {"type": "Point", "coordinates": [2, 1]},
                }
            ],
        },
    )

    (line,) = await HttpStockClient(catalog_url).reserve(order_id, ITEMS)

    assert (line.listing_id, line.farm_id, line.farm_owner_id) == (TOMATO, FARM_A, FARMER_A)
    assert line.name == "Tomato (Roma)"
    assert line.pickup_location == {"type": "Point", "coordinates": [2, 1]}
    assert str(line.unit_price) == "40.5"
    assert RECEIVED == [
        (
            "POST",
            "/internal/stock/reserve",
            {"orderId": str(order_id), "items": [{"listingId": str(TOMATO), "qty": 2.0}]},
        )
    ]


async def test_reserve_passes_catalogs_409_through(catalog_url):
    details = [{"listingId": str(TOMATO), "requested": 2, "available": 1}]
    REPLIES[RESERVE] = (
        409,
        {"error": {"code": "STOCK_UNAVAILABLE", "message": "Not enough stock", "details": details}},
    )

    with pytest.raises(AppError) as error:
        await HttpStockClient(catalog_url).reserve(uuid4(), ITEMS)

    assert (error.value.code, error.value.status_code) == ("STOCK_UNAVAILABLE", 409)
    assert error.value.details == details


async def test_reserve_passes_other_catalog_codes_through(catalog_url):
    REPLIES[RESERVE] = (409, {"error": {"code": "LISTING_NOT_AVAILABLE", "message": "Paused"}})

    with pytest.raises(AppError) as error:
        await HttpStockClient(catalog_url).reserve(uuid4(), ITEMS)

    assert error.value.code == "LISTING_NOT_AVAILABLE"


async def test_reserve_maps_other_failures_to_catalog_error(catalog_url):
    REPLIES[RESERVE] = (500, None)

    with pytest.raises(AppError) as error:
        await HttpStockClient(catalog_url).reserve(uuid4(), ITEMS)

    assert (error.value.code, error.value.status_code) == ("CATALOG_ERROR", 502)


async def test_unreachable_catalog_is_a_503():
    with pytest.raises(AppError) as error:
        await HttpStockClient("http://127.0.0.1:1", timeout=1).reserve(uuid4(), ITEMS)

    assert (error.value.code, error.value.status_code) == ("CATALOG_UNAVAILABLE", 503)


async def test_release_posts_the_order_id(catalog_url):
    order_id = uuid4()
    REPLIES[("POST", "/internal/stock/release")] = (200, {"orderId": str(order_id), "items": []})

    await HttpStockClient(catalog_url).release(order_id)

    assert RECEIVED == [("POST", "/internal/stock/release", {"orderId": str(order_id)})]
