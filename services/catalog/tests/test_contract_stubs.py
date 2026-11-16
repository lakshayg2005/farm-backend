"""The contract stubs answer every route with data that matches the published models."""

from uuid import uuid4

FARM = {
    "name": "Sahu Organic Farm",
    "district": "Durg",
    "location": {"type": "Point", "coordinates": [81.5107, 21.2665]},
}
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[81.5, 21.26], [81.52, 21.26], [81.52, 21.27], [81.5, 21.27], [81.5, 21.26]]],
}


async def test_crops_are_public(client):
    response = await client.get("/crops")

    assert response.status_code == 200
    crops = {c["id"]: c for c in response.json()}
    assert crops["tomato"] == {
        "id": "tomato",
        "name": "Tomato",
        "category": "vegetable",
        "defaultUnit": "kg",
    }


async def test_create_farm_needs_farmer_role(client, customer):
    assert (await client.post("/farms", json=FARM)).status_code == 401
    response = await client.post("/farms", json=FARM, headers=customer)
    assert response.status_code == 403


async def test_create_farm_echoes_body(client, farmer):
    response = await client.post("/farms", json={**FARM, "boundary": SQUARE}, headers=farmer)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["location"] == FARM["location"]
    assert body["boundary"] == SQUARE
    assert {"id", "farmerId", "createdAt", "updatedAt"} <= body.keys()


async def test_farm_location_is_validated(client, farmer):
    bad_point = {**FARM, "location": {"type": "Point", "coordinates": [200, 21]}}
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }

    for body in (bad_point, {**FARM, "boundary": bowtie}):
        response = await client.post("/farms", json=body, headers=farmer)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_listing_routes(client, farmer):
    listing = {
        "farmId": str(uuid4()),
        "cropId": "tomato",
        "qtyAvailable": 50,
        "pricePerUnit": 30,
        "harvestDate": "2026-11-14",
    }
    created = await client.post(
        "/listings", json=listing, headers={**farmer, "Idempotency-Key": "listing-0001"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["grade"] is None

    listing_id = created.json()["id"]
    assert (await client.get(f"/listings/{listing_id}")).status_code == 200
    assert (await client.get("/listings/mine", headers=farmer)).status_code == 200
    patched = await client.patch(
        f"/listings/{listing_id}", json={"pricePerUnit": 25, "status": "PAUSED"}, headers=farmer
    )
    assert patched.json()["pricePerUnit"] == 25
    assert patched.json()["status"] == "PAUSED"
    deleted = await client.delete(f"/listings/{listing_id}", headers=farmer)
    assert deleted.status_code == 204


async def test_cannot_set_sold_out_or_archived_by_patch(client, farmer):
    response = await client.patch(
        f"/listings/{uuid4()}", json={"status": "SOLD_OUT"}, headers=farmer
    )
    assert response.status_code == 422


async def test_search_returns_distance(client):
    response = await client.get(
        "/listings/search",
        params={"lat": 21.25, "lng": 81.6, "radius_km": 15, "crop": "tomato", "grade": "B"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    feature = body["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Point"
    assert feature["properties"]["distanceM"] > 0
    assert feature["id"] == feature["properties"]["id"]


async def test_search_requires_a_point(client):
    response = await client.get("/listings/search", params={"lat": 21.25})
    assert response.status_code == 422


async def test_internal_reserve_and_release(client):
    order_id, listing_id = str(uuid4()), str(uuid4())
    reserved = await client.post(
        "/internal/stock/reserve",
        json={"orderId": order_id, "items": [{"listingId": listing_id, "qty": 2}]},
    )
    assert reserved.status_code == 200
    item = reserved.json()["items"][0]
    assert item["listingId"] == listing_id
    assert {"unitPrice", "farmId", "farmerId", "pickupLocation"} <= item.keys()

    released = await client.post("/internal/stock/release", json={"orderId": order_id})
    assert released.status_code == 200
    assert released.json()["orderId"] == order_id


async def test_internal_geo_reference(client):
    response = await client.get(f"/internal/geo/farm/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["point"]["type"] == "Point"
    assert (await client.get(f"/internal/geo/plot/{uuid4()}")).status_code == 422
