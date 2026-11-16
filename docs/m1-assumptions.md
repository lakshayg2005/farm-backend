# M1 assumptions (order service)

Decisions made while building the order service that other issues or teams may need to confirm.
Each one is also marked `TODO(M1)` in the code where it matters. Change the contract here first,
then in `services/order/src/order_service/schemas.py`, then regenerate
`contracts/openapi/order.json`.

## Needs a decision from the team leads

| # | Assumption | Why it matters |
| --- | --- | --- |
| 1 | The rating endpoint is `POST /orders/{orderId}/rating` in the order service (score 1-5, optional comment), allowed only once the order is COMPLETED. | Mobile #8 says "rate after COMPLETED", and HLD section 12 says both sides can rate, but no backend issue has a rating route. If logistics owns ratings instead, move it there. |
| 2 | Event payloads for the `order.*` events are not defined anywhere yet. The order service defines them (camelCase) with #13. | Quality, logistics, notification and catalog consume them. |

## Contract choices (low risk, flag if you disagree)

- The cart lives only in the app. No `/cart` routes (the HLD data model lists a `carts` table; the team decision was app-only). Items are sent in `POST /orders`.
- `POST /orders` returns a `Checkout` holding a list of per-farm `Order`s. The `Idempotency-Key` header is required.
- JSON is camelCase through `farm_common.schemas.ApiModel`, as the HLD and the catalog do.
- Statuses follow HLD section 11: `PLACED, CONFIRMED, PACKED, QC_PASSED, READY_FOR_DISPATCH, POOLED, IN_TRANSIT, DELIVERED, COMPLETED, CANCELLED`.
- Quantities are called `qty`, like the catalog's stock API. Money is a number in rupees with two decimals; `currency` is always "INR".
- Lists use cursor pagination (`cursor`, `limit`, response `nextCursor`) as in HLD section 15, and `GET /orders` takes `role` and `status` filters.
- Places are GeoJSON points (`{"type": "Point", "coordinates": [lng, lat]}`, HLD section 15), the same JSON as the catalog. `deliveryAddress.location` is the drop point the logistics pooling job (#19) clusters on; every order item carries the farm's `pickupLocation`, copied from catalog when stock is reserved.
- Orders have a `deliveryDate` (the customer may choose it; the default is tomorrow) because pooling groups orders due the same day (HLD section 14).
- Payments follow the HLD payments table: a `kind` of FULL, DEPOSIT, BALANCE or REFUND (REFUND is written by the order service, clients cannot ask for it) and an optional `referenceId` (the reservation id for deposit and balance payments). The land service (#18) pays reservation deposits "through order payments"; `POST /orders/payments/webhook` (HLD section 15) is added with #24.
- `orderType` (PRODUCE, HARVEST) is on every order so the V2 harvest orders (#24) fit without a breaking change. `POST /orders/harvest` (HLD) is added by #24.
- Routes are `/orders...`; the gateway (#6) adds the public `/api/v1` prefix.
- `POST /orders/{id}/cancel` is not in this contract; it comes with #13.
- Only the `customer` role can call these stubs. Farmer actions and admin views come with #13.

## Added by #13 (checkout saga)

### What the order service expects from other services (please confirm with the owners)

| Owner | Expectation | If it is different |
| --- | --- | --- |
| M6, catalog (PR #32, #33) | Already read from the catalog code: `GET /listings/{id}` (farm lookup), `POST /internal/stock/reserve {orderId, items:[{listingId, qty}]}` (the reply's `pickupLocation` is kept on each order item), `POST /internal/stock/release {orderId}`, release on `order.cancelled` with `orderId`. Order gives each per-farm order its id up front and reserves one order at a time, because catalog keys a reservation by order id. | Only `order_service/stock.py` changes. |
| M7, quality (#12) | `quality.graded` payload contains `orderId`, or `subjectType: "ORDER"` plus `subjectId` (catalog already reads `subjectType`/`subjectId`). Any grade moves the order to QC_PASSED and then READY_FOR_DISPATCH. Other subject types (LISTING, HARVEST) are ignored by order. | If a failed grade should block an order (HLD section 11 mentions a lower-grade price or a refund), tell me the payload. |
| M2, logistics (#19, #20) | `order.ready_for_dispatch` carries everything pooling needs: `deliveryDate`, `deliveryAddress.location` (drop point), and per item `qty`, `unit` and `pickupLocation`. `pool.assigned` with `orderIds` (or `orderId`) moves orders to POOLED. `delivery.started` (assumed name) moves them to IN_TRANSIT. `delivery.completed` with `orderId` (or `orderIds`) moves them to DELIVERED, then COMPLETED, as the HLD says. | If logistics publishes per stop or with other keys, only `handle_event` in `service.py` changes. Missing steps are tolerated (an order can jump from READY_FOR_DISPATCH to COMPLETED). |
| M3, notification (#14) | Every `order.*` payload has `customerId`, `status` and the items, enough to write a message. | |
| M5, identity (#5) | The JWT `sub` is the user's UUID, and roles are `customer`, `farmer`, `admin` (already in `farm_common.auth`). | A non-UUID `sub` returns 401. |

### Choices made in the order service

- **Payment:** the mock gateway approves everything. To demo a failure, create a payment intent whose amount equals `MOCK_PAYMENT_FAIL_AMOUNT`. The intent amount is replaced by the real total (items + `DELIVERY_FEE_PER_ORDER`, flat 20.00 per farm order, assumed) when the checkout is paid.
- **Failed payment:** the orders are saved as CANCELLED (customers see them in their list), `order.cancelled` is published so catalog releases the stock, and the response is `402 PAYMENT_FAILED`. The app must use a **new** Idempotency-Key to retry. Repeating the same key returns the same 402.
- **Stock errors:** catalog's `409 STOCK_UNAVAILABLE` (details: `listingId`, `requested`, `available`), `409 LISTING_NOT_AVAILABLE` and `404 LISTING_NOT_FOUND` are passed through unchanged. Any reservation already made for another farm of the same cart is released.
- **Idempotency:** the key is stored per customer. The same key with a different request returns `409 IDEMPOTENCY_KEY_REUSED`; a parallel duplicate returns `409 IDEMPOTENCY_IN_PROGRESS`. If the checkout fails before anything is saved (for example out of stock), the key is forgotten so the same key can be retried.
- **Who sees what:** `GET /orders` returns the customer's own orders, a farmer's farm orders (matched by `farmerId` from catalog), and everything for an admin. `role=customer|farmer` picks one view for users with both roles. Other people's orders answer 404. Team 2's farmer screens can use this.
- **Pack:** `POST /orders/{id}/pack` (farmer of that farm, or admin) moves CONFIRMED to PACKED and publishes `order.packed`.
- **Cancel:** `POST /orders/{id}/cancel` (the customer, the farm's owner, or an admin; HLD section 15 lists farmers too) is allowed until QC_PASSED. A REFUND payment row is recorded and `order.cancelled` carries `cancelledBy` and `refundAmount`.
- **`payment.succeeded`:** published for every paid V1 checkout (`kind` FULL, with `checkoutId` and `orderIds`). Land (#18) waits for a DEPOSIT with its reservation id as `referenceId`; deposits are settled by `POST /orders/payments/webhook` (HLD section 15), which comes with #24.
- **Quality failure:** HLD section 11 says a failed inspection lets the customer accept the lower grade's price or cancel for a refund. Any `quality.graded` for an order moves it to QC_PASSED for now; the fail path needs quality's payload (M7 #12) and is left out.
- **Gateway path:** the HLD routes `/api/v1/orders...` to this service, so set `ROOT_PATH=/api/v1` for Swagger links (the shared setting suggests `/api/v1/<service>`).
- **Delivery date:** checkout takes an optional `deliveryDate` (today to 30 days ahead, India time); the default is tomorrow. The date is stored on the order and sent in the events.
- **Pickup points:** stored as GeoJSON in a JSON column (the HLD data model says POINT); the order service does no spatial queries, logistics clusters the points it receives.
- **HTTP to catalog** uses the standard library, not `httpx`, so `uv.lock` (shared) does not change.

### Things to change outside the order service later

- docker-compose (#2): the order service needs `DATABASE_URL` (its own schema user), `RABBITMQ_URL`, `CATALOG_URL`, and the JWT public key. `EVENTS_ENABLED=false` runs it without RabbitMQ.
- Run the migration with `cd services/order && uv run alembic upgrade head`.
- CI (#3) should also run `services/order/scripts/export_events.py`; tests already fail if `contracts/events/order.*.json` or `contracts/openapi/order.json` are out of date.
