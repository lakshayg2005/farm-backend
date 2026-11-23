# Demo guide

One small, believable story around Durg and Bhilai (Chhattisgarh), built through the public API by
`seed/` so every screen has real-looking data. Everything is deterministic: the same logins and the same
records every time.

> **The team VM holds no demo data** (decision of 2026-11-23): its database keeps only the admin and the
> accounts people registered themselves. Use this seed on a local stack (`scripts/reset-demo.sh`), or on
> the VM only after an explicit decision to reseed it.

## Logins

Password for every account except the admin: **`Farm@12345`**.

| Role | Phone | Who | What to look at |
| --- | --- | --- | --- |
| Admin | `+919999999999` (password `DemoAdmin@2026`) | Platform Admin | Dashboard, 2 farmers waiting in Verification, Pooling map with 5 routes |
| Farmer | `+919100000001` | Ramesh Sahu, Sahu Krishi Farm, Patan | 4 listings with photos, orders to pack, payouts, plots, crop timeline of a harvested rice field |
| Farmer | `+919100000004` | Kavita Chandrakar, Chandrakar Orchard, Kumhari | Guava, banana, maize; a plot with a cancelled request |
| Farmer | `+919100000005` | Dinesh Tandon, Tandon Grains, Jamul | Two plots with growing chana and mustard, crop updates with photos |
| Farmer (not yet verified) | `+919100000007`, `+919100000008` | Pooja Thakur, Vijay Nishad | Cannot sell until the admin verifies them |
| Customer | `+919200000001` | Aarav Mishra, Sector 6, Bhilai | Placed orders, one pending land request |
| Customer | `+919200000005` | Sanjay Dubey, Durg | An active reservation with a crop timeline and photos |
| Customer | `+919200000008` | Ritu Jain, Kohka, Bhilai | Harvested rice: a delivery order on the pooled routes |
| Delivery | `+919400000001` to `03` | Deepak Rana, Esha Khan, Farhan Ali | Today's pooled route, pickups and proof of delivery |

All other customers are `+91920000000N` (N = 1 to 10), farmers `+91910000000N` (N = 1 to 8).

## What is in the data

- **6 verified farmers, 2 waiting for verification**, 6 farms and 20 listings with 1 to 3 photos each. Prices
  and units follow the crop catalog (vegetables per kg or piece, grains per quintal).
- **7 plots (2 to 4 acres each)** drawn as polygons near the farms, each with Kharif, Rabi and Zaid seasons.
- **8 reservations at every stage:** waiting for the farmer, accepted and waiting for the deposit, rejected,
  cancelled, two growing crops (with sown, growing and flowering photos), one harvest waiting for the
  customer's delivery or buy-back choice, and one finished harvest delivered through a pooled route.
- **About 27 orders in every status:** to pack, cancelled, packed and due tomorrow (waiting for tomorrow's
  route), and the rest packed today, pooled and either delivered with the customer's code (with ratings),
  on the road, or assigned and waiting.
- **Pooling:** the real job groups the due orders. The biggest route has about ten stops across Bhilai and
  Durg, and the admin page shows the kilometres saved.
- **Photos:** about 65 uploaded through the normal signed-upload flow, so thumbnails and
  location data are real.

## Reset and reseed

Local stack (docker compose):

```bash
scripts/reset-demo.sh            # wipes the volumes, starts the stack, creates the admin, seeds (about 3 minutes)
scripts/reset-demo.sh --build    # same, rebuilding the service images first
```

The seed refuses to run on top of existing demo data. Orders are due **today** (India time) so the delivery
partners have a route and the pooling page has data: reseed on the day you present.

The team VM runs without Docker. See `~/reset-demo.sh` there (wipes the schemas, RabbitMQ, Redis and
object storage, restarts the services and creates the admin); then seed from any machine that can reach it:

```bash
SEED_ADMIN_PHONE=+919999999999 SEED_ADMIN_PASSWORD='DemoAdmin@2026' \
  uv run --package farm-seed python -m farm_seed --base-url http://10.1.75.79:4501/api/v1
```

For load and geo-search tests use the generated profile instead: `python -m farm_seed --profile volume`.

## What is real and what is staged

- Everything is created through the same API calls an app would make, so validation, events and
  notifications all run. Nothing is written to a table directly.
- Photos are openly licensed pictures from Wikimedia Commons (authors and licences in
  `seed/assets/CREDITS.md`). They are generic crop photos, not pictures of these invented farms.
- A demo photo claims it was taken where its farm or plot is: the seeder writes GPS and a capture time into
  the file before uploading it, so the platform's 2 km check can run for real. Real photos from a phone
  carry their own location.
- Names, farms and villages are invented. Village points are approximate.
- Crop update dates show the day the demo was seeded (the API stamps them when they are created), while the
  photo itself records an earlier capture time.

## Demo order that tells the story

1. **Admin:** dashboard numbers, verify Vijay Nishad, open Pooling, pick the biggest route on the map.
2. **Farmer (Ramesh):** listings with verified photos, pack a waiting order, look at payouts and the rice
   crop timeline.
3. **No quality gate:** packing makes an order ready for a delivery partner at once. Quality is judged by the customer: they rate each delivered order, and the farm's rating shows on its listings.
4. **Customer on the web (Sanjay):** search land on the map, open the active reservation and its photo
   timeline.
5. **Customer app (Aarav):** browse produce, check out, follow an order on the status rail, rate it.
6. **Delivery app (Deepak):** open the route, pick up, deliver with a photo and the customer's OTP.
