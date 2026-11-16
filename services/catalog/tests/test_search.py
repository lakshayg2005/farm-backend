import pytest
from sqlalchemy import text

KM = 0.009  # degrees of latitude per km (roughly)
HOME = (21.19, 81.28)  # same as conftest.HOME


@pytest.fixture
async def spread(api, client):
    """Listings 2 km, 6 km and 30 km north of HOME, with different crops, prices and grades."""
    near = await api.listing(
        await api.farm(HOME[0] + 2 * KM, HOME[1]), pricePerUnit=40, isOrganic=True
    )
    mid = await api.listing(await api.farm(HOME[0] + 6 * KM, HOME[1]), pricePerUnit=20)
    onion = await api.listing(
        await api.farm(HOME[0] + 6.5 * KM, HOME[1]), cropId="onion", pricePerUnit=15
    )
    far = await api.listing(await api.farm(HOME[0] + 30 * KM, HOME[1]))
    return {"near": near, "mid": mid, "onion": onion, "far": far}


async def search(client, **params):
    response = await client.get(
        "/listings/search", params={"lat": HOME[0], "lng": HOME[1], **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def ids(page) -> list[str]:
    return [feature["id"] for feature in page["features"]]


async def test_nearest_first_within_radius(client, spread):
    page = await search(client, radius_km=10)

    assert page["type"] == "FeatureCollection"
    assert ids(page) == [spread["near"]["id"], spread["mid"]["id"], spread["onion"]["id"]]
    first = page["features"][0]
    assert first["type"] == "Feature"
    assert first["geometry"] == spread["near"]["pickupLocation"]
    assert first["properties"]["cropId"] == "tomato"
    assert "pickupLocation" not in first["properties"]
    distances = [feature["properties"]["distanceM"] for feature in page["features"]]
    assert 1900 < distances[0] < 2100
    assert 5800 < distances[1] < 6300


async def test_filters(client, spread):
    assert ids(await search(client, crop="onion")) == [spread["onion"]["id"]]
    assert ids(await search(client, max_price=25, crop="tomato")) == [spread["mid"]["id"]]
    assert ids(await search(client, min_price=30)) == [spread["near"]["id"]]
    assert ids(await search(client, organic="true")) == [spread["near"]["id"]]
    assert ids(await search(client, radius_km=50, sort="price"))[0] == spread["onion"]["id"]


async def test_grade_filter_is_a_minimum(client, spread, session_factory):
    async with session_factory() as session:
        await session.execute(
            text("UPDATE catalog.produce_listings SET grade = 'B' WHERE id = :id"),
            {"id": spread["mid"]["id"]},
        )
        await session.execute(
            text("UPDATE catalog.produce_listings SET grade = 'A' WHERE id = :id"),
            {"id": spread["near"]["id"]},
        )
        await session.commit()

    assert ids(await search(client, grade="A")) == [spread["near"]["id"]]
    assert ids(await search(client, grade="B")) == [spread["near"]["id"], spread["mid"]["id"]]


async def test_hides_paused_and_sold_out(api, client, spread):
    await client.patch(
        f"/listings/{spread['near']['id']}", json={"status": "PAUSED"}, headers=api.farmer
    )
    await client.patch(
        f"/listings/{spread['mid']['id']}", json={"qtyAvailable": 0}, headers=api.farmer
    )
    assert ids(await search(client)) == [spread["onion"]["id"]]


async def test_pagination(client, spread):
    first = await search(client, limit=2)
    assert first["nextCursor"] == "2"
    second = await search(client, limit=2, cursor=first["nextCursor"])
    assert second["nextCursor"] is None
    assert ids(first) + ids(second) == [
        spread["near"]["id"],
        spread["mid"]["id"],
        spread["onion"]["id"],
    ]


async def test_search_uses_the_gist_index(session_factory):
    """The query's geography(pickup_location) must match the geography index expression."""
    async with session_factory() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = await session.execute(
            text(
                "EXPLAIN SELECT id FROM catalog.produce_listings WHERE ST_DWithin("
                "geography(pickup_location), "
                "geography(ST_SetSRID(ST_MakePoint(81.28, 21.19), 4326)), 10000)"
            )
        )
        assert "listings_pickup_geog_gix" in "\n".join(row[0] for row in plan)


async def test_search_validation(client):
    response = await client.get("/listings/search", params={"lat": 21.2})
    assert response.status_code == 422
    response = await client.get(
        "/listings/search", params={"lat": 21.2, "lng": 81.3, "radius_km": 500}
    )
    assert response.status_code == 422
