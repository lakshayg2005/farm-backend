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

Behind the gateway every route below is under `/api/v1/media` (HLD section 15).

| Method | Route | Who | Purpose |
| --- | --- | --- | --- |
| POST | `/upload-url` | Any logged-in user | Signed PUT URL for one JPEG/PNG/WebP (≤ 5 MB, 5 minutes); accepts `Idempotency-Key` |
| POST | `/{id}/complete` | Uploader | After the PUT: checks the file, queues processing (202) |
| GET | `/{id}` | Public | Status, `url`, `thumbUrl`, `exifTime`, `isGeoVerified`, `geoStatus`, `distanceM` (never the GPS point) |
| GET | `/owners/{ownerType}/{ownerId}` | Public | READY photos of a farm / listing / plot / reservation |
| DELETE | `/{id}` | Uploader / admin | Delete the photo and its copies |
| GET | `/internal/media/{id}` | Services | Same plus `takenAt` (GeoJSON point) and a 5-minute URL to the original |

### Client flow (HLD section 12, "Upload a real photo")

```text
1. POST /upload-url {ownerType: "FARM", ownerId, contentType: "image/jpeg", sizeBytes}
     -> {mediaId, uploadUrl, headers, expiresAt}
2. PUT uploadUrl, body = the file, headers = exactly `headers`
     (Content-Type and Content-Length are signed: a different type or size gets 403 from storage)
3. POST /{mediaId}/complete      -> 202 {status: "PROCESSING"}
4. poll GET /{mediaId} until status is READY (usually about a second), or listen for `media.processed`
```

Send the original camera file: the server reads its EXIF. Resizing or "compressing" on the
phone first usually drops the GPS, and the photo is then stored with `NO_GPS`.

### Data (HLD section 10)

`media.media`: `id, owner_service, owner_id, url, thumb_url, exif_time, is_geo_verified`,
`taken_at POINT` (PostGIS, GiST-indexed), plus `owner_type`, `status`, `geo_status` (why it is or
isn't verified), `distance_m` and upload bookkeeping. `owner_service` follows `ownerType`:
FARM/LISTING → catalog, PLOT/RESERVATION → land, DELIVERY → logistics,
USER → identity.

### The worker (arq, HLD section 8)

Processing runs as arq jobs on Redis in a separate process:

```bash
uv run --package farm-media arq media_service.worker.WorkerSettings
```

For each photo it:

- rejects files that are not real JPEG/PNG/WebP images, or don't match the declared type;
- reads the EXIF capture time and GPS with exifread (no time zone in EXIF → `EXIF_DEFAULT_TZ`,
  Asia/Kolkata) and stores the GPS as `taken_at`;
- asks the owning service where the farm or plot is (`GET /internal/geo/{kind}/{id}` on catalog
  for FARM/LISTING, on land for PLOT/RESERVATION) and checks, in PostGIS,
  `ST_DWithin(taken_at::geography, boundary::geography, 2000)` (HLD section 13; the point is used
  when there is no boundary): `VERIFIED` or `TOO_FAR`, else `NO_GPS` / `NO_REFERENCE`;
- writes `public/<id>.jpg` (≤ 1600 px) and `thumbs/<id>.jpg` (≤ 400 px) with Pillow, re-encoded with
  no EXIF, so GPS never leaves the server. `originals/` stays private;
- publishes `media.processed` (schema in `contracts/events/`).

A failing job is retried with a growing delay, and after 3 tries the photo is `FAILED`. Every
minute a cron job re-queues photos stuck in `PROCESSING`, for example when Redis was down at
`/complete`.

### Settings

`S3_ENDPOINT`, `S3_PUBLIC_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` (created on
startup, with anonymous read on `public/` and `thumbs/` only), `MEDIA_PUBLIC_BASE_URL` (CDN,
optional), `REDIS_URL`, `JOBS_ENABLED`, `CATALOG_INTERNAL_URL`, `LAND_INTERNAL_URL`,
`GEO_VERIFY_RADIUS_M` (2000), `EVENTS_ENABLED`.

### Tests

Like catalog, the tests need PostGIS (`TEST_DATABASE_URL`, see `services/catalog/README.md`).
Object storage, Redis and the catalog/land lookups are faked; the tests call the real arq job
function. Run them with `uv run pytest services/media`.
