"""Idempotency-Key on create calls (HLD sections 6 and 15)."""

import asyncio
from uuid import uuid4

from sqlalchemy import func, select

from catalog_service.db import Farm, Listing

FARM = {
    "name": "Sahu Farm",
    "district": "Durg",
    "location": {"type": "Point", "coordinates": [81.28, 21.19]},
}


async def count(session_factory, model) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_retry_with_same_key_returns_the_first_farm(api, client, session_factory):
    headers = {**api.farmer, "Idempotency-Key": "farm-create-0001"}

    first = await client.post("/farms", json=FARM, headers=headers)
    again = await client.post("/farms", json=FARM, headers=headers)

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert await count(session_factory, Farm) == 1


async def test_same_key_different_body_is_refused(api, client):
    headers = {**api.farmer, "Idempotency-Key": "farm-create-0002"}
    await client.post("/farms", json=FARM, headers=headers)

    response = await client.post("/farms", json={**FARM, "name": "Other"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_keys_are_per_user(api, client, make_token, session_factory):
    key = {"Idempotency-Key": "shared-key-0003"}
    await client.post("/farms", json=FARM, headers={**api.farmer, **key})
    other = await client.post("/farms", json=FARM, headers={**make_token(uuid4(), "farmer"), **key})

    assert other.status_code == 201
    assert await count(session_factory, Farm) == 2


async def test_without_a_key_each_call_creates(api, client, session_factory):
    await client.post("/farms", json=FARM, headers=api.farmer)
    await client.post("/farms", json=FARM, headers=api.farmer)
    assert await count(session_factory, Farm) == 2


async def test_parallel_listing_retries_create_one_listing(api, client, session_factory, outbox):
    farm = await api.farm()
    body = {
        "farmId": farm["id"],
        "cropId": "tomato",
        "qtyAvailable": 10,
        "pricePerUnit": 20,
        "harvestDate": "2026-11-14",
    }
    headers = {**api.farmer, "Idempotency-Key": "listing-create-0004"}

    responses = await asyncio.gather(
        *(client.post("/listings", json=body, headers=headers) for _ in range(6))
    )

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert await count(session_factory, Listing) == 1
    assert len(await outbox("listing.published")) == 1


async def test_short_keys_are_rejected(api, client):
    response = await client.post(
        "/farms", json=FARM, headers={**api.farmer, "Idempotency-Key": "x"}
    )
    assert response.status_code == 422
