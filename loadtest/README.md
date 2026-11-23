# Load tests (k6)

Simulated people using the platform through the public API, to find how many users it supports
and what runs out first. Results stream into Grafana (**Farm · Load test & capacity**), next to
the server-side metrics and traces (see `infra/observability/README.md`).

```bash
# k6 v2.x on your machine; admin account to verify the test farmers
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh smoke     # 5 users, 1 minute: does everything work?
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh load      # 50 -> 100 -> 200 users, ~20 min
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh stress    # up to 1500 users in steps, ~22 min
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh spike     # 50 -> 800 users in 20 s, then back
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh soak      # 150 users for 30 min (leaks, slow growth)
ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh auth      # sign-ins per second, 1 -> 40 /s
STAGES="1m:30,3m:30,1m:0" ... loadtest/run.sh load                # any custom ramp
```

`run.sh` opens an SSH tunnel to the VM's Prometheus and tags the run with a test id (pick it in
the dashboard's **Test run** box). Summaries and logs go to `loadtest/results/` (not committed).

## Who the virtual users are

Each virtual user signs in once (like opening the app), refreshes its token after 12 minutes and
then repeats a session with 1-4 s of thinking between steps:

| Share | Role | Session |
|---|---|---|
| 85% | customer | crops, unread count, produce search near home, two listings with photos, a farm and its listings; 1 in 4 sessions buys (payment intent + checkout; half are cancelled again); 1 in 5 looks at land (search, plot, seasons); orders, notifications, profile |
| 10% | farmer | profile, orders, listings, farms, payouts, plots, reservations, notifications; sometimes changes a price |
| 5% | delivery partner | route offers, today's route, three GPS pings |

One virtual user makes about 0.6 requests a second, so 100 users ≈ 60 requests/s.

## Safety: test data stays apart from real data

- Every test account has a phone number starting **+917000** and a name starting "LoadTest".
- Test farms and plots are around **Bilaspur**, ~120 km from the real Durg data, so real users do
  not see them, and customers **only order load-test listings** (stock 1,000,000 kg).
- Delivery partners ping from **Patna**, so they are never offered a real route; the test never
  runs pooling, never packs orders and never uploads photos.
- The gateway limits each client IP to 50 requests/s (5/s for sign-in). One k6 machine is one
  IP, so above ~70 users the limiter, not the backend, is what you measure (429 responses).

## Clean up afterwards

On the VM (owner database credentials come from `~/farm-db-env.sh`):

```bash
. ~/farm-db-env.sh
python loadtest/db_counts.py snapshot ~/backups/counts-before.json     # BEFORE the test
python loadtest/cleanup.py --phone-prefix +917000                      # dry run: what would go
python loadtest/cleanup.py --phone-prefix +917000 --apply              # delete it
python loadtest/db_counts.py compare ~/backups/counts-before.json      # business tables match?
```

`cleanup.py` starts from the test users and follows every table that refers to them (farms,
listings, plots, reservations, orders, payments, stock movements, notifications, pings, pools,
media and photo files, outbox and processed-event rows). It refuses to run if the selection
contains an admin or if a delivery pool mixes test and real orders.
