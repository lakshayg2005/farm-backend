from uuid import uuid4

from catalog_service.consumers import make_handler
from farm_common.events import Event, InMemoryEventBus


async def test_order_cancelled_releases_stock_once(api, session_factory):
    listing = await api.listing(qtyAvailable=10)
    order_id = uuid4()
    await api.reserve(order_id, (listing["id"], 4))
    handle = make_handler(session_factory)
    event = Event(type="order.cancelled", producer="order", payload={"orderId": str(order_id)})

    await handle(event)
    await handle(event)  # redelivered: ignored

    assert (await api.get(listing["id"]))["qtyAvailable"] == 10


async def test_quality_graded_sets_listing_grade(api, session_factory, outbox):
    a = await api.listing()
    b = await api.listing()
    handle = make_handler(session_factory)

    await handle(
        Event(
            type="quality.graded",
            producer="quality",
            payload={"subjectType": "LISTING", "subjectId": a["id"], "grade": "B"},
        )
    )
    await handle(
        Event(
            type="quality.graded",
            producer="quality",
            payload={
                "subjectType": "ORDER",
                "subjectId": str(uuid4()),
                "grade": "A",
                "listingIds": [b["id"]],
            },
        )
    )

    assert (await api.get(a["id"]))["grade"] == "B"
    assert (await api.get(b["id"]))["grade"] == "A"
    graded = [e.payload for e in await outbox("listing.updated")]
    assert {(p["listingId"], p["grade"]) for p in graded} == {(a["id"], "B"), (b["id"], "A")}


async def test_failed_grade_leaves_listing_alone(api, session_factory):
    listing = await api.listing()
    handle = make_handler(session_factory)

    await handle(
        Event(
            type="quality.graded",
            producer="quality",
            payload={"subjectType": "LISTING", "subjectId": listing["id"], "grade": "FAIL"},
        )
    )
    assert (await api.get(listing["id"]))["grade"] is None


async def test_handler_works_on_the_bus(api, session_factory):
    listing = await api.listing(qtyAvailable=5)
    order_id = uuid4()
    await api.reserve(order_id, (listing["id"], 5))
    bus = InMemoryEventBus()
    await bus.subscribe(["order.cancelled", "quality.graded"], make_handler(session_factory))

    await bus.publish(
        Event(type="order.cancelled", producer="order", payload={"orderId": str(order_id)})
    )

    assert (await api.get(listing["id"]))["qtyAvailable"] == 5
