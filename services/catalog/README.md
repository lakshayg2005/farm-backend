# catalog service

**Owner:** Team 2

V1 farms and produce listings, geo search, stock reserve/release.

| | |
| --- | --- |
| Python package | `catalog_service` |
| Local dev port | 8002 |
| Database schema | `catalog` |

```bash
# from the repo root
uv run --package farm-catalog uvicorn catalog_service.main:app --reload --port 8002
# migrations (needs DATABASE_URL)
cd services/catalog && uv run alembic revision --autogenerate -m "describe change"
cd services/catalog && uv run alembic upgrade head
```

## API (contract: [`contracts/openapi/catalog.json`](../../contracts/openapi/catalog.json))

Behind the gateway every route below is under `/api/v1/catalog` (routes as in HLD section 15).
JSON bodies are camelCase; query parameters are as the HLD writes them (`radius_km`, `max_price`).
Locations are GeoJSON (`{"type": "Point", "coordinates": [lng, lat]}`), and search returns a
GeoJSON `FeatureCollection` (HLD section 13). Other lists use `?cursor&limit` and return
`{"items": [...], "nextCursor": "..."}`. `POST /farms` and `POST /listings` accept an
`Idempotency-Key` header (HLD sections 6 and 15).

| Method | Route | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/crops` | Public | Crop reference list (ids like `tomato` are shared with land) |
| POST | `/farms` | Farmer | Create a farm (point + optional boundary polygon) |
| GET | `/farms/mine` | Farmer | My farms |
| GET, PATCH | `/farms/{id}` | Public / owner | Farm details, edit |
| GET | `/farms/{id}/listings` | Public | A farm's active listings |
| POST | `/listings` | Farmer | Publish produce (publishes `listing.published`) |
| GET | `/listings/mine?status&farm_id` | Farmer | My listings in every status |
| GET | `/listings/search?lat&lng&radius_km&crop&min_price&max_price&grade&organic&sort` | Public | Produce near a point: GeoJSON features with `distanceM` |
| GET, PATCH, DELETE | `/listings/{id}` | Public / owner | Details, edit price/stock/pause (`listing.updated`), archive |

Internal (Docker network only, never through the gateway):

| Method | Route | Caller | Purpose |
| --- | --- | --- | --- |
| POST | `/internal/stock/reserve` | order | Reserve stock for an order: all items or none, idempotent per `orderId`; 409 `STOCK_UNAVAILABLE` |
| POST | `/internal/stock/release` | order | Give back an order's reserved stock (also done on `order.cancelled`) |
| GET | `/internal/geo/{farm\|listing}/{id}` | media | Point and boundary, for photo geo-verification |

Events: publishes `listing.published`, `listing.updated` (schemas in `contracts/events/`); consumes
`order.cancelled` (releases stock) and `quality.graded` (sets the listing grade).

Regenerate the contract after changing routes or models:

```bash
uv run python -c "import json; from catalog_service.main import app; \
print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > contracts/openapi/catalog.json
```

## How stock stays correct

- Reserve is one conditional `UPDATE ... SET qty_available = qty_available - :qty WHERE qty_available >= :qty`
  per listing, in id order (no deadlocks), all items in one transaction (all or nothing).
- `CHECK (qty_available >= 0)` makes overselling impossible even if code is wrong.
- `stock_reservations` (one row per order) makes reserve and release idempotent: a retried reserve
  returns the original items and prices; a release before/after reserve blocks a late reserve.
- `stock_movements` is the ledger: for every listing, the sum of deltas equals `qty_available`.
- ACTIVE flips to SOLD_OUT at 0 stock and back when stock returns (`listing.updated` both times).

Creating farms and listings with an `Idempotency-Key` stores the first response in
`idempotency_keys` (per user), in the same transaction as the new row; a retry gets that response
back, and a parallel duplicate loses on the primary key and also gets it back.

## Tests

The tests need PostgreSQL + PostGIS (geo search and row locking can't be faked with SQLite):

```bash
docker run -d --rm --name farm-test-db -p 55433:5432 -e POSTGRES_USER=farm \
    -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=farm_test postgis/postgis:16-3.4
export TEST_DATABASE_URL=postgresql+asyncpg://farm:change-me@localhost:55433/farm_test
uv run pytest services/catalog
```

The fixture drops and re-creates the `catalog` schema with the Alembic migrations. Without
`TEST_DATABASE_URL` the tests are skipped locally and fail when `CI` is set.
