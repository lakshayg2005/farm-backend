# Demo script (about 12 minutes)

Run `scripts/reset-demo.sh` (or `~/reset-demo.sh` plus the seed on the team VM) **on the day you present**: the orders
are due today, so the delivery partners have a route and the pooling page has data. Logins are in [DEMO.md](DEMO.md).
Open the website in a desktop window and, for the mobile moments, a 390 px wide window (browser dev tools).

Two stories, told in the order the platform was designed for (HLD section 5).

## V1: from the farmer's field to the table (6 minutes)

1. **Customer (app).** Sign in as Aarav, +919200000001. Browse produce near Bhilai, add tomato from Sahu Krishi Farm and
   cauliflower from a second farm, pay. Two orders appear, one per farm. *Say:* one checkout, one order per farm.
2. **Farmer (web).** Sign in as Ramesh, +919100000001. **Orders → To pack** shows the new order and the stage bar at 2 of 6.
   Open **Notifications**: "New order to pack". Press **Mark packed**; the bar moves to 3 and the order is ready for a
   delivery partner (there is no quality gate: the customer's rating after delivery is the quality signal).
3. **Photos.** *Say:* photos keep their GPS and capture time. Show a listing photo.
4. **Admin (web).** Sign in as +919999999999 (password in DEMO.md). **Dashboard**: orders per day, sales, customer
   rating. **Pooling**: pick today, **Run pooling**; routes appear on the map with kilometres saved against separate trips.
   **Orders → Needs a step**: where anything is stuck. *Say:* an admin can cancel and refund from here.
5. **Delivery partner (app).** Deepak, +919400000001: today's route, pickups, then a drop with a photo and the 4-digit
   delivery code. Only the customer sees the code (in **My orders**); the partner's route never shows it, and a wrong
   code is refused.
6. **Customer and farmer again.** The order reads Delivered, the customer rates it, the farmer's **Earnings** page lists the
   payout under the month.

## V2: reserve a field and follow the crop (6 minutes)

1. **Customer (web).** Sanjay, +919200000005, **Find land**: search the map, open a plot, see seasons and the price per kg.
   Try to reserve a season that has ended: the API refuses it ("This season has already ended").
2. **Farmer (web).** Sunita Verma, +919100000002, **Requests**: Aarav's request is waiting; the 48 hour timer is visible. Accept it.
   The customer then pays the deposit and the reservation turns **Active** (this crosses the land and order services).
3. **Crops (farmer).** Dinesh, +919100000005, **Crops** (his chana and mustard are already growing): stage bar Sown, Growing, Flowering, Harvest ready; **Post an update** with a photo.
4. **Customer.** **My reservations → Growing**: the same timeline with the farmer's photos. *Say:* the customer follows the
   crop from a phone without calling anyone.
5. **Harvest.** The farmer marks the crop harvested with its weight; the harvest order appears at once. Ritu's rice
   (+919200000008) went out on a pooled route. The alternative, **buy-back**, is a choice on **My reservations**: the
   customer pays the balance and is credited 90% of the harvest's value; the page shows both amounts first.

## If something goes wrong

- A page says "Could not load": every list has a Try again button; check `GET /api/v1/<service>/health`.
- No photos: the media worker or object storage is down (`docker compose ps`; on the VM `~/start-minio.sh`).
- Pooling shows nothing: the orders are not due today. Reseed.
- An order did not move after packing: RabbitMQ is the hand-off between services; check its queues for a backlog.

## Proof that it works without you

- `uv run --package farm-seed python -m farm_seed.journeys` walks V1 and V2 through the API with an assertion at every
  status change (about 30 seconds), and runs in CI on an empty stack.
- `pnpm e2e` in farm-web drives every role's pages at 390 and 1440 px, and fails on any console error or 5xx.
