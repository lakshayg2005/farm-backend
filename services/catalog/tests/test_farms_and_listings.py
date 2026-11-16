from uuid import uuid4

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[81.27, 21.18], [81.29, 21.18], [81.29, 21.2], [81.27, 21.2], [81.27, 21.18]]],
}


async def test_crops_are_seeded(client):
    response = await client.get("/crops")

    assert response.status_code == 200
    crops = {c["id"]: c for c in response.json()}
    assert len(crops) > 30
    assert crops["tomato"] == {
        "id": "tomato",
        "name": "Tomato",
        "category": "vegetable",
        "defaultUnit": "kg",
    }


async def test_farm_crud(api, client):
    farm = await api.farm(boundary=SQUARE, village="Kumhari")

    assert farm["farmerId"] == str(api.farmer_id)
    assert farm["location"] == {"type": "Point", "coordinates": [81.28, 21.19]}
    assert farm["boundary"]["type"] == "MultiPolygon"
    assert farm["boundary"]["coordinates"][0] == SQUARE["coordinates"]

    assert (await client.get(f"/farms/{farm['id']}")).json()["name"] == "Sahu Farm"
    mine = await client.get("/farms/mine", headers=api.farmer)
    assert [f["id"] for f in mine.json()["items"]] == [farm["id"]]

    patched = await client.patch(
        f"/farms/{farm['id']}", json={"name": "Sahu Organic", "boundary": None}, headers=api.farmer
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Sahu Organic"
    assert patched.json()["boundary"] is None


async def test_only_owner_or_admin_edits_a_farm(api, client, make_token):
    farm = await api.farm()
    other = make_token(uuid4(), "farmer")
    admin = make_token(uuid4(), "admin")

    response = await client.patch(f"/farms/{farm['id']}", json={"name": "Mine"}, headers=other)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_FARM_OWNER"
    response = await client.patch(f"/farms/{farm['id']}", json={"name": "Ok"}, headers=admin)
    assert response.status_code == 200


async def test_unknown_farm_is_404(client):
    response = await client.get(f"/farms/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FARM_NOT_FOUND"


async def test_create_listing_publishes_event(api, outbox):
    farm = await api.farm()
    listing = await api.listing(farm, variety="Desi", isOrganic=True, qtyAvailable=12.5)

    assert listing["farmName"] == "Sahu Farm"
    assert listing["cropName"] == "Tomato"
    assert listing["qtyAvailable"] == 12.5
    assert listing["status"] == "ACTIVE"
    assert listing["grade"] is None
    assert listing["pickupLocation"] == farm["location"]

    events = await outbox("listing.published")
    assert len(events) == 1
    assert events[0].payload["listingId"] == listing["id"]
    assert events[0].payload["qtyAvailable"] == 12.5


async def test_listing_on_someone_elses_farm_is_refused(api, client, make_token):
    farm = await api.farm()
    body = {
        "farmId": farm["id"],
        "cropId": "tomato",
        "qtyAvailable": 5,
        "pricePerUnit": 10,
        "harvestDate": "2026-11-14",
    }
    response = await client.post("/listings", json=body, headers=make_token(uuid4(), "farmer"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_FARM_OWNER"


async def test_unknown_crop_is_rejected(api, client):
    farm = await api.farm()
    body = {
        "farmId": farm["id"],
        "cropId": "dragonfruit",
        "qtyAvailable": 5,
        "pricePerUnit": 10,
        "harvestDate": "2026-11-14",
    }
    response = await client.post("/listings", json=body, headers=api.farmer)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNKNOWN_CROP"


async def test_patch_listing_price_stock_and_pause(api, client, outbox):
    listing = await api.listing()
    url = f"/listings/{listing['id']}"

    response = await client.patch(
        url, json={"pricePerUnit": 25.5, "qtyAvailable": 40}, headers=api.farmer
    )
    assert response.status_code == 200, response.text
    assert response.json()["pricePerUnit"] == 25.5
    assert response.json()["qtyAvailable"] == 40

    paused = await client.patch(url, json={"status": "PAUSED"}, headers=api.farmer)
    assert paused.json()["status"] == "PAUSED"
    sold_out = await client.patch(
        url, json={"status": "ACTIVE", "qtyAvailable": 0}, headers=api.farmer
    )
    assert sold_out.json()["status"] == "SOLD_OUT"
    restocked = await client.patch(url, json={"qtyAvailable": 3}, headers=api.farmer)
    assert restocked.json()["status"] == "ACTIVE"

    assert len(await outbox("listing.updated")) == 4


async def test_patch_rejects_null_for_required_fields(api, client):
    listing = await api.listing()
    response = await client.patch(
        f"/listings/{listing['id']}", json={"pricePerUnit": None}, headers=api.farmer
    )
    assert response.status_code == 422


async def test_archive_listing(api, client):
    listing = await api.listing()
    url = f"/listings/{listing['id']}"

    assert (await client.delete(url, headers=api.farmer)).status_code == 204
    assert (await client.get(url)).status_code == 404
    response = await client.patch(url, json={"pricePerUnit": 5}, headers=api.farmer)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LISTING_ARCHIVED"

    mine = await client.get("/listings/mine", headers=api.farmer)
    assert mine.json()["items"] == []
    archived = await client.get("/listings/mine?status=ARCHIVED", headers=api.farmer)
    assert [i["id"] for i in archived.json()["items"]] == [listing["id"]]


async def test_other_farmer_cannot_edit_listing(api, client, make_token):
    listing = await api.listing()
    response = await client.patch(
        f"/listings/{listing['id']}",
        json={"pricePerUnit": 1},
        headers=make_token(uuid4(), "farmer"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_LISTING_OWNER"


async def test_farm_listings_shows_active_only_and_paginates(api, client):
    farm = await api.farm()
    ids = [(await api.listing(farm))["id"] for _ in range(3)]
    await client.patch(f"/listings/{ids[0]}", json={"status": "PAUSED"}, headers=api.farmer)

    page1 = (await client.get(f"/farms/{farm['id']}/listings?limit=1")).json()
    assert page1["nextCursor"] == "1"
    page2 = (await client.get(f"/farms/{farm['id']}/listings?limit=1&cursor=1")).json()
    assert page2["nextCursor"] is None
    assert {page1["items"][0]["id"], page2["items"][0]["id"]} == set(ids[1:])


async def test_geo_reference_for_media(api, client):
    farm = await api.farm(boundary=SQUARE)
    listing = await api.listing(farm)

    by_farm = (await client.get(f"/internal/geo/farm/{farm['id']}")).json()
    by_listing = (await client.get(f"/internal/geo/listing/{listing['id']}")).json()
    assert by_farm["point"] == farm["location"]
    assert by_farm["boundary"]["type"] == "MultiPolygon"
    assert by_listing == by_farm
    assert (await client.get(f"/internal/geo/farm/{uuid4()}")).status_code == 404


async def test_validation_errors(api, client):
    farm = {
        "name": "X",
        "district": "Durg",
        "location": {"type": "Point", "coordinates": [200, 21]},
    }
    response = await client.post("/farms", json=farm, headers=api.farmer)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    farm["location"]["coordinates"] = [81.28, 21.19]
    response = await client.post("/farms", json={**farm, "boundary": bowtie}, headers=api.farmer)
    assert response.status_code == 422


async def test_roles_are_enforced(client, make_token):
    body = {"name": "X", "district": "Durg", "location": {"type": "Point", "coordinates": [81, 21]}}
    assert (await client.post("/farms", json=body)).status_code == 401
    customer = make_token(uuid4(), "customer")
    assert (await client.post("/farms", json=body, headers=customer)).status_code == 403
