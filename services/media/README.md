# media service

**Owner:** Team 2

Signed photo uploads, EXIF/GPS verification, thumbnails.

| | |
| --- | --- |
| Python package | `media_service` |
| Local dev port | 8004 |
| Database schema | `media` |

```bash
# from the repo root
uv run --package farm-media uvicorn media_service.main:app --reload --port 8004
# migrations (needs DATABASE_URL)
cd services/media && uv run alembic revision --autogenerate -m "describe change"
cd services/media && uv run alembic upgrade head
```

## API (contract: [`contracts/openapi/media.json`](../../contracts/openapi/media.json))

Behind the gateway every route below is under `/api/v1/media`.

| Method | Route | Who | Purpose |
| --- | --- | --- | --- |
| POST | `/upload-url` | Any logged-in user | Signed PUT URL for one JPEG/PNG/WebP (≤ 5 MB, 5 minutes) |
| POST | `/{id}/complete` | Uploader | After the PUT: checks the file, queues processing (202) |
| GET | `/{id}` | Public | Status, public URL, thumbnail, `geoStatus`, `distanceM` (never the GPS point) |
| GET | `/owners/{ownerType}/{ownerId}` | Public | READY photos of a farm / listing / plot / reservation |
| DELETE | `/{id}` | Uploader / admin | Delete the photo and its copies |
| GET | `/internal/media/{id}` | Services | Same plus raw GPS and a 5-minute URL to the original |

### Client flow

```text
1. POST /upload-url {ownerType: "FARM", ownerId, contentType: "image/jpeg", sizeBytes}
     -> {mediaId, uploadUrl, headers, expiresAt}
2. PUT uploadUrl, body = the file, headers = exactly `headers`
     (Content-Type and Content-Length are signed: a different type or size gets 403 from storage)
3. POST /{mediaId}/complete      -> 202 {status: "PROCESSING"}
4. poll GET /{mediaId} until status is READY (usually < 1 s), or listen for `media.processed`
```

Send the original camera file: the server reads its EXIF. Resizing or "compressing" on the
phone first usually drops the GPS, and the photo then shows as `NO_GPS` (unverified).

### What the worker does

- Rejects files that are not real JPEG/PNG/WebP images, or don't match the declared type.
- Reads the EXIF capture time (no time zone in EXIF → `EXIF_DEFAULT_TZ`, Asia/Kolkata) and GPS.
- Asks the owning service where the farm/plot is (`GET /internal/geo/{kind}/{id}` on catalog
  for FARM/LISTING, on land for PLOT/RESERVATION) and computes the distance to its boundary
  (or point): ≤ 2 km → `VERIFIED`, else `TOO_FAR`; `NO_GPS`; `NO_REFERENCE` if the lookup failed.
- Writes `public/<id>.jpg` (≤ 1600 px) and `thumbs/<id>.jpg` (≤ 400 px), re-encoded with no
  EXIF, so GPS never leaves the server. `originals/` stays private.
- Publishes `media.processed` (schema in `contracts/events/`). Retries 3 times, then `FAILED`.

The worker runs inside the service (`WORKER_ENABLED=true`) or on its own with
`uv run --package farm-media python -m media_service.worker`; rows are claimed with
`FOR UPDATE SKIP LOCKED`, so several can run.

### Settings

`S3_ENDPOINT`, `S3_PUBLIC_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` (created on
startup, with anonymous read on `public/` and `thumbs/` only), `MEDIA_PUBLIC_BASE_URL` (CDN,
optional), `CATALOG_INTERNAL_URL`, `LAND_INTERNAL_URL`, `GEO_VERIFY_RADIUS_M` (2000),
`WORKER_ENABLED`, `EVENTS_ENABLED`. Tests use SQLite and an in-memory store: `uv run pytest services/media`.
