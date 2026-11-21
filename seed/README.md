# seed

Demo data for the platform, built only through the gateway API (so it passes the same validation and
publishes the same events as real traffic). It has two profiles:

| Profile | Command | What it is for |
| --- | --- | --- |
| `demo` (default) | `scripts/reset-demo.sh` | A small, believable story around Durg and Bhilai: named farmers and farms, real openly licensed photos with GPS, plots, reservations at every stage, orders in every status, inspections, pooled routes and deliveries. See [docs/DEMO.md](../docs/DEMO.md) for the logins and the tour. |
| `volume` | `python -m farm_seed --profile volume ...` | Generated people and orders in bulk, for load and geo-search tests. Not meant to be shown. |

The demo profile needs an **empty** database (it refuses to run if the demo farmer already exists):
`scripts/reset-demo.sh` wipes the docker compose volumes, starts the stack, creates the admin and seeds.
Photos live in `seed/assets/photos` with their authors and licences in `seed/assets/CREDITS.md`;
`seed/tools/fetch_photos.py` re-downloads them from Wikimedia Commons.

The rest of this file describes the `volume` profile.

Dummy data simulator. Uses a fixed random seed and calls the gateway API with an admin token, so
seeded data passes the same validation and publishes the same events as real traffic. Running it
twice is safe: existing accounts and farms are reused.

**V1 (this version):** 20 verified farmers in 8 districts, their farms, 3 to 6 produce listings per
farm, and 40 customers. Every account's password is `Farm@12345` (dev only); phones are
`+919000100000...` (farmers) and `+919000200000...` (customers).

```bash
# the stack must be running (docker-compose, issue #2) and an admin must exist:
#   uv run --package farm-identity python -m identity_service.cli create-admin ...
export SEED_ADMIN_PHONE=+919999999999 SEED_ADMIN_PASSWORD=change-me-please
uv run --package farm-seed python -m farm_seed                 # defaults to http://localhost/api/v1
uv run --package farm-seed python -m farm_seed --farmers 5 --customers 10
```

**V2 and orders (run after V1 in the same command):** 1 to 3 plots per farmer (square plots a few acres
each, with a Kharif and a Rabi season), reservations taken to different stages (waiting, accepted,
active with deposit paid and a few crop updates), and produce orders (`--orders`, default 200; use
`--orders 2000` for the full demo set). `--skip-v2` seeds V1 only. Plots are created only for farmers
who have none yet, so re-running does not duplicate them. Orders are placed again on every run
(each order needs a fresh payment), so run with `--orders 0` when re-running.

**Operations (runs last, `--skip-ops` to leave it out):** takes up to 90 of the placed orders through the
whole flow with API calls only, so every app has data: some stay CONFIRMED, some are cancelled, some
are packed and wait in the inspector queue, some inspections fail (re-harvest, or lower grade
accepted), and the passed ones become READY_FOR_DISPATCH. Then it runs the real pooling job
(`POST /logistics/pools/run`) and delivery partners work the pools: some delivered, some with pickups
done (in progress), some untouched. It also creates 3 inspectors (`+919000300001` to `03`) and 4
delivery partners (`+919000400001` to `04`), same password, with the inspector role granted by the
admin. The steps that rely on events poll for up to 90 seconds, so RabbitMQ and the workers must be
up. Run it on a fresh database: it only picks up orders that are still CONFIRMED.

Not seeded yet: photos (media needs object storage), so inspection reports and proof of delivery
carry no real photos (delivery uses a random media id).
Reservations become ACTIVE only once RabbitMQ delivers the deposit event, so crop updates for a
just-paid reservation can be skipped by the API; the seeder ignores that.
