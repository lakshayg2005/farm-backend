# Overhaul roadmap (November 2026)

Goal: web, apps and backend behave as one product; no fake data anywhere; one curated demo
dataset; a clean, consistent UI on web and mobile; every planned feature (HLD section 5, F1 to F15)
verified end to end.

## Status

| Phase | What | State |
| --- | --- | --- |
| 0 | Sync repos to `main`, clean stack on an empty volume | Done locally |
| 1 | Live audit of every feature against the real backend | Started: stack boots from scratch, existing seed runs end to end (V1, V2, operations) |
| 2 | Sync: remove fake data and mock logins, regenerate API types, working photos | Next |
| 3 | Curated demo seed and `docs/DEMO.md` | |
| 4 | Web redesign: direction C "Ledger" chosen | Ready to start after Phase 2 |
| 5 | Mobile redesign: Kotlin app, handed to the Android session ([android-handoff.md](android-handoff.md)) | Brief written, direction C tokens included |
| 6 | End-to-end tests (Playwright, API flows), screenshots, VM deploy, demo script | |
| 7 | New features | |

## Phase 1 findings so far

- All eight services report healthy behind the gateway on a fresh database.
- The existing seed (6 farmers, 12 customers, 30 orders) completes in about 45 s: 46 listings,
  12 plots, 10 reservations, 34 orders packed, 26 inspections reported, 7 pools, 3 delivered.
- Seeded reservations produce **no crop updates** (`crop_updates=0`): a reservation only becomes
  ACTIVE after the deposit event round trip, and the seeder does not wait for it. The demo seed
  must wait for ACTIVE and then post updates, otherwise the crop timeline is empty everywhere.
- No photos are seeded, so inspection reports, listings and delivery proof carry none. Needs object
  storage on every environment, and real photos in the demo seed.
- `GET /catalog/listings/search` rejects `radius_km` above 100 (422): clients must clamp.
- Client problems found by reading code: silent fallbacks to bundled fake data in both mobile
  stacks, a reservation that is fabricated locally when the API call fails (Kotlin), mock login
  buttons, hard-coded placeholder text ("Local Village", "Assigned Farmer"), no call to the crop
  updates endpoint, and a gateway URL hard-coded in `gradle.properties`.

## Phase 2: sync (clients and backend agree)

1. Delete every fake-data path and mock login in web and mobile (see android-handoff.md section 5).
2. Run `pnpm gen:api` in the web repo against the current specs (payouts and agreements landed in
   backend #62) and fix payload drift.
3. Object storage on the team VM (MinIO community build), then verify upload, thumbnail and GPS
   verification end to end from the web.
4. Add typed error and empty states everywhere a request can fail.
5. Fix each defect from Phase 1 in the owning repo, one PR per issue.

## Phase 3: demo seed

A small curated story instead of Faker volume: about 10 farmers with believable names in 2 to 3
districts, real crop photos (openly licensed, credited), about 25 customers, 3 inspectors, 4
delivery partners, orders and reservations spread over every status, easy-to-remember demo logins
per role, and a `--volume` flag for load and geo-search tests. Wipe databases, object storage and
RabbitMQ queues first (the VM wipe is approved).

## Phase 4 and 5: UI

Design tokens, a small shared component set, and rebuilt screens per role, with loading, empty and
error states and layouts from 360 px up. Web first (React, Tailwind, Motion for transitions only
where they explain a change). The Android session follows the same direction.

## Phase 6: verification

Playwright journeys per role, two scripted API journeys (V1: listing, order, pack, inspection,
pool, delivery, rating. V2: plot, reservation, deposit, crop updates, harvest, inspection,
delivery or buy-back), screenshots at 390 px and 1440 px, deploy to the VM, written demo script.
Closes backend #22 and #26.

## Phase 7: new features

| Priority | Feature |
| --- | --- |
| P1 | Farmer earnings ledger page (payouts endpoint exists since #62) |
| P1 | Notification inbox with unread badge on web |
| P1 | Admin dashboard with real numbers: orders per day, sales, inspection pass rate, km saved by pooling |
| P1 | Admin orders view with dispute and refund action |
| P2 | Farm ratings shown on listings |
| P2 | Low-stock alert, auto-pause listing at zero stock |
| P2 | Hindi and English |
| P2 | Inspector screens usable on a phone browser |
| P3 | Receipts and agreement PDFs, reorder and favourite farms, partner earnings, weather on the crop timeline |
