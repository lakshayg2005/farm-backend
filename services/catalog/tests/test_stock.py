import asyncio
import random
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from catalog_service.db import Listing, StockMovement


async def test_reserve_returns_order_item_details(api):
    listing = await api.listing(qtyAvailable=10, pricePerUnit=32)
    order_id = uuid4()

    response = await api.reserve(order_id, (listing["id"], 4))

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["listingId"] == listing["id"]
    assert item["qty"] == 4
    assert item["unitPrice"] == 32
    assert item["farmerId"] == str(api.farmer_id)
    assert item["pickupLocation"] == listing["pickupLocation"]
    assert (await api.get(listing["id"]))["qtyAvailable"] == 6


async def test_reserve_is_all_or_nothing(api):
    a = await api.listing(qtyAvailable=10)
    b = await api.listing(qtyAvailable=1)

    response = await api.reserve(uuid4(), (a["id"], 5), (b["id"], 2))

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "STOCK_UNAVAILABLE"
    assert error["details"] == [{"listingId": b["id"], "requested": 2.0, "available": 1.0}]
    assert (await api.get(a["id"]))["qtyAvailable"] == 10


async def test_duplicate_lines_are_merged(api):
    listing = await api.listing(qtyAvailable=5)
    response = await api.reserve(uuid4(), (listing["id"], 3), (listing["id"], 3))
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["requested"] == 6.0


async def test_reserve_is_idempotent_per_order(api, client):
    listing = await api.listing(qtyAvailable=10, pricePerUnit=30)
    order_id = uuid4()

    first = await api.reserve(order_id, (listing["id"], 4))
    await client.patch(f"/listings/{listing['id']}", json={"pricePerUnit": 99}, headers=api.farmer)
    again = await api.reserve(order_id, (listing["id"], 4))

    assert again.status_code == 200
    assert again.json() == first.json()  # same qty and the original price
    assert (await api.get(listing["id"]))["qtyAvailable"] == 6


async def test_paused_and_missing_listings(api, client):
    listing = await api.listing()
    await client.patch(f"/listings/{listing['id']}", json={"status": "PAUSED"}, headers=api.farmer)

    paused = await api.reserve(uuid4(), (listing["id"], 1))
    assert paused.status_code == 409
    assert paused.json()["error"]["code"] == "LISTING_NOT_AVAILABLE"
    missing = await api.reserve(uuid4(), (str(uuid4()), 1))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "LISTING_NOT_FOUND"


async def test_sold_out_and_release(api, outbox):
    listing = await api.listing(qtyAvailable=3)
    order_id = uuid4()

    await api.reserve(order_id, (listing["id"], 3))
    assert (await api.get(listing["id"]))["status"] == "SOLD_OUT"

    released = await api.release(order_id)
    assert released.json()["items"] == [{"listingId": listing["id"], "qty": 3.0}]
    after = await api.get(listing["id"])
    assert after["qtyAvailable"] == 3
    assert after["status"] == "ACTIVE"
    assert [e.payload["status"] for e in await outbox("listing.updated")] == [
        "SOLD_OUT",
        "ACTIVE",
    ]

    # twice is harmless, and a late retry of the reserve is refused
    assert (await api.release(order_id)).json()["items"] == []
    late = await api.reserve(order_id, (listing["id"], 1))
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "ORDER_RELEASED"


async def test_release_before_reserve_blocks_the_reserve(api):
    listing = await api.listing(qtyAvailable=3)
    order_id = uuid4()

    assert (await api.release(order_id)).json()["items"] == []
    assert (await api.reserve(order_id, (listing["id"], 1))).status_code == 409
    assert (await api.get(listing["id"]))["qtyAvailable"] == 3


async def test_database_refuses_negative_stock(api, session_factory):
    listing = await api.listing(qtyAvailable=1)
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="qty_available_non_negative"):
            await session.execute(
                text("UPDATE catalog.produce_listings SET qty_available = -1 WHERE id = :id"),
                {"id": listing["id"]},
            )


# ---- concurrency: "stock never goes negative under concurrent tests" -----------------------


async def test_concurrent_checkouts_never_oversell(api, session_factory):
    listing = await api.listing(qtyAvailable=25)

    responses = await asyncio.gather(*(api.reserve(uuid4(), (listing["id"], 1)) for _ in range(60)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 25
    assert codes.count(409) == 35
    final = await api.get(listing["id"])
    assert final["qtyAvailable"] == 0
    assert final["status"] == "SOLD_OUT"
    await assert_ledger_matches(session_factory)


async def test_concurrent_multi_item_orders_with_releases(api, session_factory):
    """Random baskets over 3 listings plus random cancellations, all at once."""
    stock = {(await api.listing(qtyAvailable=q))["id"]: q for q in (20, 15, 30)}
    listing_ids = list(stock)
    rng = random.Random(7)
    orders = [
        (uuid4(), [(lid, rng.randint(1, 6)) for lid in rng.sample(listing_ids, rng.randint(1, 3))])
        for _ in range(50)
    ]

    async def checkout(order_id, items):
        response = await api.reserve(order_id, *items)
        if response.status_code == 200 and rng.random() < 0.3:
            await api.release(order_id)  # payment failed: compensate
        return response

    results = await asyncio.gather(*(checkout(o, items) for o, items in orders))

    assert all(r.status_code in (200, 409) for r in results)
    assert any(r.status_code == 409 for r in results)
    async with session_factory() as session:
        quantities = dict((await session.execute(select(Listing.id, Listing.qty_available))).all())
    assert all(qty >= 0 for qty in quantities.values())
    await assert_ledger_matches(session_factory)


async def test_concurrent_retries_of_the_same_order_reserve_once(api):
    listing = await api.listing(qtyAvailable=10)
    order_id = uuid4()

    responses = await asyncio.gather(*(api.reserve(order_id, (listing["id"], 3)) for _ in range(8)))

    assert all(r.status_code == 200 for r in responses)
    assert (await api.get(listing["id"]))["qtyAvailable"] == 7


async def assert_ledger_matches(session_factory) -> None:
    """For every listing, the sum of stock movements equals qty_available."""
    async with session_factory() as session:
        rows = await session.execute(
            select(
                Listing.id, Listing.qty_available, func.coalesce(func.sum(StockMovement.delta), 0)
            )
            .join(StockMovement, StockMovement.listing_id == Listing.id, isouter=True)
            .group_by(Listing.id)
        )
        for listing_id, qty, ledger in rows:
            assert qty >= 0
            assert Decimal(qty) == Decimal(ledger), listing_id
