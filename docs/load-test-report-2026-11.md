# Load test and end-to-end report (team VM, 24 Nov 2026)

What we measured on the team VM (`10.1.75.79`) and what to fix before more users arrive. Live
numbers are in Grafana at `https://10.1.75.79:3501/grafana/` (see `infra/observability/README.md`);
how to run the tests again is in `loadtest/README.md`.

## The machine

The VM is a container limited to **10 CPU cores and 15 GB of RAM** (the host shows 120 cores, but
that is not ours). Each service is **one Python process**, so it can use at most one core for
its own work. PostgreSQL allows **100 connections**.

## Results

All runs went through the gateway from one client machine (k6), with realistic users: 85%
customers, 10% farmers, 5% delivery partners, 1-4 s of thinking between steps (about 0.5
requests per second per user).

| Run | Users | Requests/s | p95 | p99 | Failed |
|---|---|---|---|---|---|
| Smoke | 5 | 3 | 211 ms | 594 ms | 0% |
| Load (within gateway limits) | 30 → 60 | 29 avg, 49 peak | **113 ms** | 423 ms | **0%** (19,273 requests, 19,375 checks passed) |
| Sign-ins at 4/s, before the fix | n/a | 4 | 30 s (timeouts) | 30 s | **16.5%** |
| Sign-ins at 4/s, after the fix | n/a | 4 | 1.19 s | 2.39 s | **0%** |

At 60 users the VM used at most 48% of its CPU, and the database peaked at **50 of its 100
connections**.

**Not measured yet: the breaking point.** The gateway limits each client IP to 50 requests/s
(5/s for sign-in). The load tests come from one IP, so above about 70 users we would measure the
limiter, not the platform. The stress and spike runs (up to 1,500 users) need the limits raised
for the length of the test (see "Next" below).

## Findings

1. **Sign-in froze the identity service. Fixed in this branch.** `bcrypt` took about 0.5 s of CPU
   inside the async login and register handlers, so the whole identity service stopped for each
   sign-in. Traces showed a 500 ms gap between the user lookup and the token insert. At 4
   sign-ins/s requests piled up until they timed out. Now bcrypt runs in a thread (it releases
   the GIL), so sign-ins run in parallel on several cores (identity used 2.15 cores at peak) and
   nothing else waits behind them. A sign-in still costs ~0.6 s by design.
2. **The database will run out of connections first.** Every service keeps up to 5 + 10
   connections, so 7 services can open 105 against a limit of 100, before counting the workers,
   the exporter and psql. At only 49 requests/s we already saw 50. Options: lower `pool_size` /
   `max_overflow` per service (most requests take a few ms of database time), raise
   `max_connections` to 200, or put PgBouncer in front.
3. **Every web user shares one rate-limit bucket.** nginx forwards the website's API calls from
   127.0.0.1, so the gateway's per-IP limit (50 requests/s, 5 sign-ins/s) applies to **all
   website users together**. A class of 20 students signing in at once would get 429 errors.
   Fix: have Traefik use the `X-Forwarded-For` that nginx sets (`sourceCriterion.ipStrategy.depth:
   1` on the web router), or rate-limit per user.
4. **Every database call costs an extra round trip.** `pool_pre_ping=True` runs `BEGIN; SELECT 1;
   ROLLBACK` before each checkout; the dashboard shows more rollbacks than commits. Cheap at this
   size; worth turning off or replacing with `pool_recycle` when Postgres is local.
5. **Checkout is the slowest common request (p95 434 ms)**: idempotency row, payment lookup,
   stock reservation over HTTP to the catalog, then six inserts. The order service calls the
   catalog with `urllib` in a thread; its spans now appear in traces too.
6. **Minor: marking someone else's notification as read returns 200 with a made-up
   notification** (a leftover stub fallback in the notification service). Nothing is changed or
   leaked, but it should be 404.

## End-to-end check (all passed)

- Journeys V1 and V2 (`seed/src/farm_seed/journeys.py`), run around Bilaspur, away from real
  delivery partners: verification gate, listing, paid order and stock, notifications, packing,
  pooling, route offer and accept, OTP + photo delivery, rating, payout; land plots, reservation,
  deposit, crop update with photo, harvest, buy-back, pooled harvest delivery.
- `loadtest/api_checks.py`, 54 checks: sessions (refresh rotation, logout, OTP), admin-only
  routes, gateway blocking `/internal`, ownership checks, edits and deletes, order cancel
  releasing stock, idempotent checkout, reservation reject/cancel, overlapping seasons, notification
  read state, devices, the media lifecycle (upload, processing, thumbnail, delete).
- Website (Playwright, 390 px and 1440 px): sign-in, shop, cart, orders, find land,
  reservations, notifications; no console errors, no 5xx responses.
- After the runs: no dead-lettered events, no queued messages, no new error logs.

## Test data

Removed. The database was backed up first (`~/backups/farm-before-loadtest-2026-11-24.dump`,
and again before cleanup). `loadtest/cleanup.py` deleted 208 test users and everything linked to
them (384 orders, 84 listings, 14 plots, 1,407 notifications, 6 photos and their files, outbox
and processed-event rows, ...). Every business table is back at its count from before the tests,
except rows added by real users during the test (2 location pings and 2 sign-ins by a delivery
partner) and 12 sign-in sessions of the admin account that the tests used.

## Next

1. Raise the gateway limits for the stress and spike runs, run them, then restore the limits.
   This gives the breaking point and shows which resource fails first.
2. Fix findings 2 and 3, then run the stress test again.
3. Try `UVICORN_WORKERS=2..4` for catalog and order (the busiest services) and compare.
