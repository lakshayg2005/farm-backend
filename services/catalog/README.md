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

Behind the gateway every route below is under `/api/v1/catalog`. JSON is camelCase; locations are
GeoJSON (`{"type": "Point", "coordinates": [lng, lat]}`). Lists use `?cursor&limit` and return
`{"items": [...], "nextCursor": "..."}`.

| Method | Route | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/crops` | Public | Crop reference list (ids like `tomato` are shared with land) |
| POST | `/farms` | Farmer | Create a farm (point + optional boundary polygon) |
| GET | `/farms/mine` | Farmer | My farms |
| GET, PATCH | `/farms/{id}` | Public / owner | Farm details, edit |
| GET | `/farms/{id}/listings` | Public | A farm's active listings |
| POST | `/listings` | Farmer | Publish produce (publishes `listing.published`) |
| GET | `/listings/mine?status&farmId` | Farmer | My listings in every status |
| GET | `/listings/search?lat&lng&radiusKm&crop&minPrice&maxPrice&grade&organic&sort` | Public | Produce near a point, with `distanceM` |
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
