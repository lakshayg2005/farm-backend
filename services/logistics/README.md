# logistics service

**Owner:** Team 1 · Slot M2 · Sprint 3 (Issue #19)

Delivery pooling, routes, partner app API, tracking, and proof of delivery.

| | |
| --- | --- |
| Python package | `logistics_service` |
| Local dev port | 8007 |
| Database schema | `logistics` |
| Route prefix | `/api/v1/logistics` |

---

## Features & Architecture

### 1. Delivery Pooling Job (`POST /pools/run` & Nightly Cron)
- **Clustering**: Groups orders ready for dispatch using **DBSCAN** (`eps ≈ 3.0 km`, haversine metric) on delivery drop coordinates.
- **Capacity Splitting**: Divides oversized clusters into sub-clusters when total weight exceeds partner capacity (`partner_capacity_kg = 50.0 kg`).
- **Pickup-Before-Drop Constraint**: Sequenced so hub pickup stops precede customer drop stops.
- **OSRM Routing Integration**: Calls the OSRM Trip API for the drop order and `km_saved`, then the Route API through the stops in their real visiting order (all pickups, then all drops) for the road line, `total_km` and per-drop ETAs. The line is stored on the pool and returned as `routePath` ([lat, lng] pairs) by `GET /my-route`; clients fall back to straight lines when it is absent. Point `OSRM_BASE_URL` at your own router (`infra/osrm/`); the public demo server is rate-limited and has no uptime guarantee.
- **Event Publishing**: Publishes `pool.assigned` with pool metadata and stops via the transactional outbox (`farm_common.outbox`).

### 2. Worker & crons
- An **arq** worker (`logistics_service.worker.WorkerSettings`) runs two crons:
  - **02:00 IST** (`20:30 UTC`): safety net that pools any IMMEDIATE order missed on packing.
  - **10:00 IST** (`BATCH_HOUR_IST`/`BATCH_MINUTE_IST`): the **shared-delivery batch**, see below.
- Can also be triggered on-demand via `POST /api/v1/logistics/pools/run?pool_date=YYYY-MM-DD[&mode=IMMEDIATE|FLEXIBLE]`
  (no `mode` pools both).

### 3. Shared-delivery batch (FLEXIBLE orders)
A customer who chose "I can wait" has `deliveryMode = FLEXIBLE`. Packing such an order sends
`order.ready_for_dispatch` as usual, but the consumer **does not** start a pooling run for it: the
order waits. At the batch time the worker fetches every waiting FLEXIBLE order due that day or
earlier (`GET order/internal/orders/ready-for-dispatch?mode=FLEXIBLE`) and pools them with wider
settings than an immediate run: drops within `BATCH_DBSCAN_EPS_KM` (5 km) and pickups within
`BATCH_HUB_EPS_KM` (30 km). A cluster over capacity or over `MAX_DROPS_PER_POOL` is cut along its
longest axis, so pools stay compact. The new pools are offered to partners exactly like any other
(first to accept wins). Admins can preview the queue and its routes with `GET /pools/batch`, and run
it early with `POST /pools/run?mode=FLEXIBLE`.

---

## Local Development & Testing

```bash
# Start API locally
uv run --package farm-logistics uvicorn logistics_service.main:app --reload --port 8007

# Run worker locally
uv run --package farm-logistics arq logistics_service.worker.WorkerSettings

# Run migrations (requires DATABASE_URL)
cd services/logistics && uv run alembic upgrade head

# Run tests
uv run pytest services/logistics
```

## Dispatch: route offers (first to accept gets it)

When a farmer packs an order, the order service publishes `order.ready_for_dispatch`. Logistics
waits `POOL_ON_READY_DELAY_S` (5 s, so orders packed together share a pool) and runs the pooling
job for that order's delivery date. With `DISPATCH_MODE=offer` (the default) each new pool is not
assigned; it is **offered**:

1. Partners with a location ping in the last `OFFER_ONLINE_MINUTES` (30) and within
   `OFFER_RADIUS_KM` (15) of the first pickup get an offer, nearest first. Nobody that close: every
   online partner; nobody online: every known partner. `pool.offered` goes out, and the
   notification service pushes `POOL_OFFER` to each of them.
2. `POST /logistics/offers/{poolId}/accept` assigns the pool with one conditional UPDATE
   (`WHERE partner_id IS NULL`), so of two partners accepting at once exactly one wins; the other
   gets `409 POOL_TAKEN`. The winner's response is the route.
3. `pool.assigned` then goes out as before (orders become POOLED, the customer is told), and
   `pool.offer_closed` makes the notification service replace the other partners' offer
   notification with "Route taken" (same FCM `tag`).
4. `GET /logistics/offers/mine` lists a partner's open offers; `POST .../decline` turns one down.

`DISPATCH_MODE=assign` keeps the old behaviour (straight to the least-loaded partner).
