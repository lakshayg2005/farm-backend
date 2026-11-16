"""Client for catalog's stock API (catalog_service/internal.py and routes.py, issues #10 / PR #33).

What order uses:

    GET  {catalog}/listings/{id}            -> {"farmId", "farmerId", ...}   (public route)
    POST {catalog}/internal/stock/reserve   {"orderId", "items": [{"listingId", "qty"}]}
         200 {"orderId", "items": [{"listingId", "farmId", "farmerId", "cropName", "variety",
                                    "unit", "qty", "unitPrice", "pickupLocation", ...}]}
         404 LISTING_NOT_FOUND, 409 STOCK_UNAVAILABLE (details: [{listingId, requested,
         available}]), 409 LISTING_NOT_AVAILABLE, 409 ORDER_RELEASED
    POST {catalog}/internal/stock/release   {"orderId"} -> 200 (idempotent)

Catalog keys a reservation by order id, and releases it when it consumes `order.cancelled`
(payload `orderId`). So checkout looks up each listing's farm first, gives every per-farm order
its id up front, and reserves one order at a time.

Uses the standard library for HTTP so the shared uv.lock does not change.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from farm_common.errors import AppError
from farm_common.log import get_correlation_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockRequestItem:
    listing_id: UUID
    qty: Decimal


@dataclass(frozen=True)
class ReservedLine:
    listing_id: UUID
    farm_id: UUID
    farm_owner_id: UUID
    name: str
    unit: str
    unit_price: Decimal
    qty: Decimal
    pickup_location: dict[str, Any]  # GeoJSON point


class StockClient(Protocol):
    async def farms_of(self, listing_ids: list[UUID]) -> dict[UUID, UUID]:
        """Farm id of each listing. Raises LISTING_NOT_FOUND (404) for an unknown listing."""
        ...

    async def reserve(self, order_id: UUID, items: list[StockRequestItem]) -> list[ReservedLine]:
        """Reserve all items for one order or none. Raises STOCK_UNAVAILABLE (409) etc."""
        ...

    async def release(self, order_id: UUID) -> None:
        """Give back everything reserved for the order. Safe to call for any order id."""
        ...


def _request(
    method: str, url: str, payload: dict[str, Any] | None, timeout: float
) -> tuple[int, Any]:
    """Blocking JSON request with the standard library. Returns (status, parsed body or None)."""
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    correlation_id = get_correlation_id()
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None


def _error(body: Any) -> dict[str, Any]:
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


class HttpStockClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        try:
            return await asyncio.to_thread(
                _request, method, f"{self.base_url}{path}", payload, self.timeout
            )
        except (OSError, ValueError) as exc:
            logger.warning("Catalog call %s %s failed: %s", method, path, exc)
            raise AppError("CATALOG_UNAVAILABLE", "Stock service is not reachable", 503) from exc

    async def farms_of(self, listing_ids: list[UUID]) -> dict[UUID, UUID]:
        replies = await asyncio.gather(*(self._call("GET", f"/listings/{i}") for i in listing_ids))
        farms: dict[UUID, UUID] = {}
        for listing_id, (status, body) in zip(listing_ids, replies, strict=True):
            if status == 404:
                raise AppError(
                    "LISTING_NOT_FOUND",
                    "A listing in the cart does not exist",
                    404,
                    details=[{"listingId": str(listing_id)}],
                )
            if status != 200 or not isinstance(body, dict) or "farmId" not in body:
                raise AppError("CATALOG_ERROR", f"Listing lookup failed with status {status}", 502)
            farms[listing_id] = UUID(body["farmId"])
        return farms

    async def reserve(self, order_id: UUID, items: list[StockRequestItem]) -> list[ReservedLine]:
        payload = {
            "orderId": str(order_id),
            "items": [{"listingId": str(i.listing_id), "qty": float(i.qty)} for i in items],
        }
        status, body = await self._call("POST", "/internal/stock/reserve", payload)
        if status in (404, 409):
            error = _error(body)
            raise AppError(
                error.get("code", "STOCK_UNAVAILABLE" if status == 409 else "LISTING_NOT_FOUND"),
                error.get("message", "Stock could not be reserved"),
                status,
                details=error.get("details"),
            )
        if status != 200 or not isinstance(body, dict):
            raise AppError("CATALOG_ERROR", f"Stock reserve failed with status {status}", 502)
        try:
            return [
                ReservedLine(
                    listing_id=UUID(line["listingId"]),
                    farm_id=UUID(line["farmId"]),
                    farm_owner_id=UUID(line["farmerId"]),
                    name=line["cropName"]
                    + (f" ({line['variety']})" if line.get("variety") else ""),
                    unit=line["unit"],
                    unit_price=Decimal(str(line["unitPrice"])),
                    qty=Decimal(str(line["qty"])),
                    pickup_location=line["pickupLocation"],
                )
                for line in body["items"]
            ]
        except (KeyError, ValueError, TypeError) as exc:
            raise AppError("CATALOG_ERROR", "Unexpected stock reserve reply", 502) from exc

    async def release(self, order_id: UUID) -> None:
        status, _ = await self._call("POST", "/internal/stock/release", {"orderId": str(order_id)})
        if status != 200:
            raise AppError("CATALOG_ERROR", f"Stock release failed with status {status}", 502)
