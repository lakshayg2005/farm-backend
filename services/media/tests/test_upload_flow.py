import io
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from PIL import Image

KM = 0.009  # degrees of latitude per km (roughly)
FARM = (21.19, 81.28)


def make_photo_at(make_photo, lat: float, lng: float) -> bytes:
    return make_photo(gps=(lat, lng))


async def test_upload_url_needs_login(client):
    body = {
        "ownerType": "FARM",
        "ownerId": str(uuid4()),
        "contentType": "image/jpeg",
        "sizeBytes": 1,
    }
    assert (await client.post("/upload-url", json=body)).status_code == 401


async def test_upload_url_validation(uploader, client):
    base = {"ownerType": "FARM", "ownerId": str(uuid4()), "contentType": "image/jpeg"}
    too_big = await client.post(
        "/upload-url", json={**base, "sizeBytes": 6 * 1024 * 1024}, headers=uploader.headers
    )
    assert too_big.status_code == 422
    gif = await client.post(
        "/upload-url",
        json={**base, "contentType": "image/gif", "sizeBytes": 10},
        headers=uploader.headers,
    )
    assert gif.status_code == 422


async def test_ticket(uploader):
    data = b"x" * 10
    ticket = await uploader.request(data)

    assert ticket["method"] == "PUT"
    assert ticket["headers"] == {"Content-Type": "image/jpeg"}
    assert f"originals/{ticket['mediaId']}.jpg" in ticket["uploadUrl"]
    expires = datetime.fromisoformat(ticket["expiresAt"])
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)


async def test_photo_at_the_farm_is_verified(uploader, store, outbox, make_photo):
    media = await uploader.upload(make_photo_at(make_photo, FARM[0] + 0.5 * KM, FARM[1]))

    assert media["status"] == "READY"
    assert media["geoStatus"] == "VERIFIED"
    assert media["isGeoVerified"] is True
    assert media["distanceM"] == 0  # inside the farm boundary
    assert media["width"] == 2400 and media["height"] == 1800
    # EXIF has no zone, so it is read as IST (EXIF_DEFAULT_TZ) and returned in UTC
    assert datetime.fromisoformat(media["takenAt"]) == datetime(
        2026, 11, 14, 7, 45, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    assert media["url"].endswith(f"public/{media['id']}.jpg")
    assert media["thumbUrl"].endswith(f"thumbs/{media['id']}.jpg")
    assert "gpsLat" not in media  # GPS is never on public routes

    events = await outbox()
    assert [e.type for e in events] == ["media.processed"]
    assert events[0].payload["isGeoVerified"] is True
    assert events[0].payload["ownerId"] == str(uploader.farm_id)


async def test_public_copies_have_no_gps(uploader, store, make_photo):
    media = await uploader.upload(make_photo_at(make_photo, *FARM))

    public = Image.open(io.BytesIO((await store.get(f"public/{media['id']}.jpg"))[0:]))
    thumb = Image.open(io.BytesIO(await store.get(f"thumbs/{media['id']}.jpg")))
    assert not public.getexif()
    assert max(public.size) == 1600
    assert max(thumb.size) == 400
    # the original keeps its GPS for audits
    original = Image.open(io.BytesIO(await store.get(f"originals/{media['id']}.jpg")))
    assert original.getexif().get_ifd(0x8825)


async def test_photo_3km_away_is_flagged(uploader, make_photo):
    # 3 km north of the farm centre = about 2.45 km from the boundary edge
    media = await uploader.upload(make_photo_at(make_photo, FARM[0] + 3 * KM, FARM[1]))

    assert media["geoStatus"] == "TOO_FAR"
    assert media["isGeoVerified"] is False
    assert 2300 < media["distanceM"] < 2600


async def test_photo_just_inside_2km_is_verified(uploader, make_photo):
    media = await uploader.upload(
        make_photo_at(make_photo, FARM[0] + 2.2 * KM, FARM[1])
    )  # ~1.65 km from edge
    assert media["geoStatus"] == "VERIFIED"
    assert 1500 < media["distanceM"] < 1800


async def test_photo_without_gps(uploader, resolver, make_photo):
    media = await uploader.upload(make_photo(gps=None))

    assert media["status"] == "READY"
    assert media["geoStatus"] == "NO_GPS"
    assert resolver.calls == []  # no need to look up the farm


async def test_unknown_farm_gives_no_reference(uploader, client, make_photo):
    data = make_photo_at(make_photo, *FARM)
    ticket = await uploader.request(data, ownerId=str(uuid4()))
    await uploader.put(ticket, data)
    await uploader.complete(ticket["mediaId"])
    await uploader.process()

    media = (await client.get(f"/{ticket['mediaId']}")).json()
    assert media["geoStatus"] == "NO_REFERENCE"


async def test_profile_photos_are_not_geo_checked(uploader, resolver, make_photo):
    media = await uploader.upload(
        make_photo_at(make_photo, *FARM), ownerType="USER", ownerId=str(uploader.user_id)
    )
    assert media["geoStatus"] == "NOT_APPLICABLE"
    assert resolver.calls == []


async def test_png_and_rotation(uploader, make_photo):
    media = await uploader.upload(make_photo(gps=None, fmt="PNG", size=(300, 200)), "image/png")
    assert media["status"] == "READY"

    rotated = await uploader.upload(make_photo(gps=None, size=(400, 300), orientation=6))
    assert (rotated["width"], rotated["height"]) == (300, 400)


async def test_fake_image_is_rejected(uploader, store):
    media = await uploader.upload(b"<?php echo 'not a photo'; ?>" * 10)

    assert media["status"] == "REJECTED"
    assert media["rejectReason"] == "NOT_AN_IMAGE"
    assert media["url"] is None
    assert not any(k.startswith("originals/") for k in store.objects)


async def test_declared_type_must_match(uploader, make_photo):
    png = make_photo(gps=None, fmt="PNG", size=(50, 50))
    media = await uploader.upload(png, "image/jpeg")
    assert media["status"] == "REJECTED"
    assert media["rejectReason"] == "TYPE_MISMATCH"


async def test_complete_before_upload(uploader):
    ticket = await uploader.request(b"x" * 10)
    response = await uploader.complete(ticket["mediaId"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPLOAD_MISSING"


async def test_oversized_upload_is_rejected_on_complete(uploader, settings):
    settings.max_upload_bytes = 1000
    ticket = await uploader.request(b"x" * 10)
    await uploader.put(ticket, b"x" * 2000)

    response = await uploader.complete(ticket["mediaId"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_complete_is_idempotent_and_owner_only(uploader, client, make_photo, make_token):
    data = make_photo(gps=None)
    ticket = await uploader.request(data)
    await uploader.put(ticket, data)

    stranger = make_token(uuid4(), "farmer")
    response = await client.post(f"/{ticket['mediaId']}/complete", headers=stranger)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_UPLOADER"

    first = await uploader.complete(ticket["mediaId"])
    again = await uploader.complete(ticket["mediaId"])
    assert first.json()["status"] == again.json()["status"] == "PROCESSING"
    assert await uploader.process()
    assert not await uploader.process()


async def test_list_and_delete(uploader, client, store, make_photo):
    first = await uploader.upload(make_photo_at(make_photo, *FARM))
    second = await uploader.upload(make_photo_at(make_photo, *FARM))

    page = (await client.get(f"/owners/FARM/{uploader.farm_id}")).json()
    assert [m["id"] for m in page["items"]] == [second["id"], first["id"]]

    assert (await client.delete(f"/{first['id']}", headers=uploader.headers)).status_code == 204
    assert (await client.get(f"/{first['id']}")).status_code == 404
    assert f"public/{first['id']}.jpg" not in store.objects
    page = (await client.get(f"/owners/FARM/{uploader.farm_id}")).json()
    assert [m["id"] for m in page["items"]] == [second["id"]]


async def test_internal_route_has_gps(uploader, client, make_photo):
    media = await uploader.upload(make_photo_at(make_photo, FARM[0] + 0.001, FARM[1]))

    internal = (await client.get(f"/internal/media/{media['id']}")).json()
    assert abs(internal["gpsLat"] - (FARM[0] + 0.001)) < 1e-5
    assert internal["originalUrl"].startswith("http")


async def test_worker_has_nothing_to_do(uploader):
    assert await uploader.process() is False
