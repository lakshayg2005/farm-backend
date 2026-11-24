# M1 assumptions (order service)

> **Update 2026-11-23 (end-to-end audit).** Several items below are now settled or void:
> - **No quality gate.** `quality.graded` is gone with the quality service. Packing moves a produce order
>   PACKED -> READY_FOR_DISPATCH in one step; `QC_PASSED` no longer exists. A customer can cancel until the
>   order is packed. The `harvest.ready` event creates the harvest order at once for `harvestedQtyKg`
>   (default `expectedQtyKg`); lower-grade pricing (`GRADE_B/C_PRICE_FACTOR`) is removed.
> - **`reservation.accepted` carries `total`, `expectedQtyKg` and `pricePerKg`** (and `harvest.ready` carries
>   `total`), so the harvest price is no longer estimated from the deposit.
> - **Buy-back charges the balance first** (a BALANCE payment), then credits the customer 90 % (REFUND) and
>   records the remaining 10 % as the farmer's PAYOUT. It used to credit 90 % with only the deposit paid.
> - **Dispatch feed** (`/internal/orders/ready-for-dispatch`) also sends `customer_id` and `contact_phone`, and
>   `weight_kg` converts units to kilograms (a quintal is 100 kg).
> - `GET /orders` rows carry `ratingScore`.

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
- `POST /orders/{id}/cancel`, the farmer actions and the admin views were added with #13.

## Added by #13 (checkout saga)

### What the order service expects from other services (please confirm with the owners)

| Owner | Expectation | If it is different |
| --- | --- | --- |
| M6, catalog (PR #32, #33) | Already read from the catalog code: `GET /listings/{id}` (farm lookup), `POST /internal/stock/reserve {orderId, items:[{listingId, qty}]}` (the reply's `pickupLocation` is kept on each order item), `POST /internal/stock/release {orderId}`, release on `order.cancelled` with `orderId`. Order gives each per-farm order its id up front and reserves one order at a time, because catalog keys a reservation by order id. | Only `order_service/stock.py` changes. |
| M7, quality (#12) | `quality.graded` payload contains `orderId`, or `subjectType: "ORDER"` plus `subjectId` (catalog already reads `subjectType`/`subjectId`). `result: "PASS"` (or no result) moves the order to QC_PASSED and then READY_FOR_DISPATCH; `result: "FAIL"` cancels it and refunds it (changed in #24, see below). LISTING subjects are ignored by order; HARVEST is handled by #24. | If a failed grade should offer the customer a lower price instead, tell me. |
| M2, logistics (#19, #20) | `order.ready_for_dispatch` carries everything pooling needs: `deliveryDate`, `deliveryAddress.location` (drop point), and per item `qty`, `unit` and `pickupLocation`. `pool.assigned` with `orderIds` (or `orderId`) moves orders to POOLED. `delivery.started` (assumed name) moves them to IN_TRANSIT. `delivery.completed` with `orderId` (or `orderIds`) moves them to DELIVERED, then COMPLETED, as the HLD says. | If logistics publishes per stop or with other keys, only `handle_event` in `consumers.py` changes. Missing steps are tolerated (an order can jump from READY_FOR_DISPATCH to COMPLETED). |
| M3, notification (#14) | Every `order.*` payload has `customerId`, `status` and the items, enough to write a message. | |
| M5, identity (#5) | The JWT `sub` is the user's UUID, and roles are `customer`, `farmer`, `admin` (already in `farm_common.auth`). | A non-UUID `sub` returns 401. |

### Choices made in the order service

- **Payment:** the mock gateway approves everything. To demo a failure, create a payment intent whose amount equals `MOCK_PAYMENT_FAIL_AMOUNT`. The intent amount is replaced by the real total (items + `DELIVERY_FEE_PER_ORDER`, flat 20.00 per farm order, assumed) when the checkout is paid.
- **Failed payment:** the orders are saved as CANCELLED (customers see them in their list), `order.cancelled` is published so catalog releases the stock, and the response is `402 PAYMENT_FAILED`. The app must use a **new** Idempotency-Key to retry. Repeating the same key returns the same 402.
- **Stock errors:** catalog's `409 STOCK_UNAVAILABLE` (details: `listingId`, `requested`, `available`), `409 LISTING_NOT_AVAILABLE` and `404 LISTING_NOT_FOUND` are passed through unchanged. Any reservation already made for another farm of the same cart is released.
- **Idempotency:** the key is stored per customer. The same key with a different request returns `409 IDEMPOTENCY_KEY_REUSED`; a parallel duplicate returns `409 IDEMPOTENCY_IN_PROGRESS`. If the checkout fails before anything is saved (for example out of stock), the key is forgotten so the same key can be retried.
- **Who sees what:** `GET /orders` returns the customer's own orders, a farmer's farm orders (matched by `farmerId` from catalog), and everything for an admin. `role=customer|farmer` picks one view for users with both roles. Other people's orders answer 404. Team 2's farmer screens can use this.
- **Pack:** `POST /orders/{id}/pack` (farmer of that farm, or admin) moves CONFIRMED to PACKED and publishes `order.packed`.
- **Cancel:** `POST /orders/{id}/cancel` (the customer, the farm's owner, or an admin; HLD section 15 lists farmers too) is allowed until the farmer packs it. An admin can also cancel while it waits for a route or is on one (READY_FOR_DISPATCH, POOLED), but not once the partner has collected it (IN_TRANSIT); logistics consumes `order.cancelled` and skips the order's open stops. A REFUND payment row is recorded and `order.cancelled` carries `cancelledBy` and `refundAmount`.
- **`payment.succeeded`:** published for every paid V1 checkout (`kind` FULL, with `checkoutId` and `orderIds`). Land (#18) waits for a DEPOSIT with its reservation id as `referenceId`; deposits are added by #24 (below).
- **Gateway path:** the HLD routes `/api/v1/orders...` to this service, so set `ROOT_PATH=/api/v1` for Swagger links (the shared setting suggests `/api/v1/<service>`).
- **Delivery date:** checkout takes an optional `deliveryDate` (today to 30 days ahead, India time); the default is tomorrow. The date is stored on the order and sent in the events.
- **Pickup points:** stored as GeoJSON in a JSON column (the HLD data model says POINT); the order service does no spatial queries, logistics clusters the points it receives.
- **HTTP to catalog** uses the standard library, not `httpx`, so `uv.lock` (shared) does not change.

### Things to change outside the order service later

- docker-compose (#2): the order service needs `DATABASE_URL` (its own schema user), `RABBITMQ_URL`, `CATALOG_URL`, and the JWT public key. `EVENTS_ENABLED=false` runs it without RabbitMQ.
- Run the migration with `cd services/order && uv run alembic upgrade head`.
- CI (#3) should also run `services/order/scripts/export_events.py`; tests already fail if `contracts/events/order.*.json` or `contracts/openapi/order.json` are out of date.

## Added by #24 (harvest orders, deposits, refunds)

Read from M7's open PR (#39): `reservation.accepted {reservationId, customerId, farmerId, deposit}`, `harvest.ready {reservationId, farmerId, customerId, cropId, expectedQtyKg, plotCentroid}`, `quality.graded {subjectType, subjectId, grade, result, resolution, weightKg}`, and land's consumer of `payment.succeeded {kind: "DEPOSIT", referenceId, amount}`. The order service matches all of them.

### How it works

1. `reservation.accepted` is remembered (table `harvest_reservations`: customer, farmer, deposit).
2. The customer pays the deposit with `POST /orders/payments/intent {amount, kind: "DEPOSIT", referenceId: <reservation id>}`. `amount` must equal the reservation's deposit (`422 DEPOSIT_AMOUNT_MISMATCH`), the reservation must belong to the caller (`404 RESERVATION_NOT_FOUND`) and can be paid once (`409 DEPOSIT_ALREADY_PAID`). The mock gateway takes it at once and the response already says `SUCCEEDED` (or `FAILED` for the test amount `MOCK_PAYMENT_FAIL_AMOUNT`). `payment.succeeded` (kind DEPOSIT, `referenceId` = reservation) is published, and land activates the reservation. With `MOCK_AUTO_SETTLE=false` the intent stays PENDING until the gateway calls `POST /orders/payments/webhook {paymentIntentId, status}` (header `X-Webhook-Secret`, setting `PAYMENT_WEBHOOK_SECRET`). The webhook is idempotent.
3. `harvest.ready` is remembered: the plot's centroid becomes the pickup point of the harvest.
4. `quality.graded` with `subjectType: "HARVEST"`:
   - `result: PASS` creates the HARVEST order (status PLACED, `harvestOption` empty, `order.placed` published).
   - `result: FAIL` with `resolution: ACCEPT_LOWER_GRADE` creates it at the lower grade price (grade B 90 %, C 80 %).
   - any other FAIL (`REHARVEST` or undecided) creates nothing; the farmer harvests again and land sends a new `harvest.ready`.
5. The customer calls `POST /orders/harvest {reservationId, option, deliveryAddress, deliveryDate}`:
   - **DELIVERY:** the order goes through CONFIRMED, PACKED, QC_PASSED to READY_FOR_DISPATCH in one step (it was graded already) and `order.ready_for_dispatch` is published, so logistics pools it like any order. The delivery fee is added. When `delivery.completed` arrives, the balance (total minus deposit) is charged (a BALANCE payment, `payment.succeeded` kind BALANCE) and the order is COMPLETED. A deposit larger than the final price is refunded instead.
   - **BUYBACK:** the customer is credited (a REFUND payment of 90 % of the harvest value) and the order is COMPLETED at once. Nothing goes to logistics.
6. `GET /orders/{id}` of any order now also lists `payments` and `balanceDue`.

### Contract changes (needed for harvest orders; produce orders are unchanged)

`Order`, `OrderSummary`: `farmId` is empty on a harvest order, new `reservationId` and `harvestOption`. `Order.deliveryAddress` and `deliveryDate` are empty until DELIVERY is chosen. `OrderItem.listingId` and `farmId` are empty on a harvest line. Same for the `order.*` events. Clients must treat these as nullable. Clients can no longer create BALANCE intents (the order service does).

### Assumptions to confirm (all marked `TODO(M1)` in the code)

| For | Assumption | If different |
| --- | --- | --- |
| M7 (land #18) | `reservation.accepted` also carries the crop price as `total` (plus `cropId`, `expectedQtyKg`). Today it does not: then the price is estimated as `deposit / HARVEST_DEPOSIT_SHARE` (20 %). The harvest order price is `total / expectedQtyKg` per kg times the graded `weightKg`. | Add `total` to the event, or tell me the deposit share. |
| M7 (land #18) | `reservation.cancelled {reservationId}` is published when a reservation is cancelled; order refunds the deposit. This event does not exist in PR #39 yet. | Name or payload differ: only `consumers.py` changes. |
| M7 (quality #12) | `quality.graded` for an ORDER with `result: FAIL` means the packed order is cancelled and refunded (`order.cancelled`, `cancelledBy: system`, reason `QUALITY_FAILED`). | Say if the customer should be offered a lower price instead. |
| Team lead | Open question in the HLD: buy-back price. Assumed 90 % of the harvest value (`BUYBACK_RATE`), paid as credit; the deposit counts towards the price and is not returned separately. | Change the setting. |
| Team lead | Open question in the HLD: deposit refund policy. Assumed: cancelling a harvest order before the option is chosen, or a cancelled reservation, refunds the whole deposit. After DELIVERY is chosen it can no longer be cancelled. | |
| M2 (logistics) | A delivered harvest order is settled when `delivery.completed` arrives (balance charged, then COMPLETED). If the balance is declined the order stays DELIVERED with a FAILED BALANCE payment. There is no retry endpoint yet. | |
| M3 (gateway #6) | `POST /orders/payments/webhook` is called by the gateway, not by a user: it must be reachable without a JWT (it checks `X-Webhook-Secret` itself). | |

### Other choices

- A harvest order is stored as a one-order checkout so the existing tables keep working; its `checkoutId` is not used by the app.
- A harvest order without a plot location (`harvest.ready` had none) gets the pickup point `[0, 0]`.
- `order.packed` is never published for a harvest order: quality would open a second inspection for a harvest that is already graded.
- Not added: an admin endpoint to refund any payment. Refunds happen on cancel, on a cancelled reservation, on a failed produce inspection, on an overpaid balance and as buy-back credit.

## Added later: feed for the logistics pooling job

Read from M2's logistics code (merged): the pooling job calls `GET {ORDER_SERVICE_URL}/internal/orders/ready-for-dispatch?date=YYYY-MM-DD` and expects a list of `{order_id, hub_lat, hub_lng, drop_lat, drop_lng, weight_kg, address}`. The order service now serves it (`internal.py`, not exposed by the gateway). It lists orders in READY_FOR_DISPATCH due on that day or earlier; `hub_*` is the first item's pickup point, `weight_kg` adds up the item quantities whatever the unit (assumed). Logistics' `pool.assigned` (`orderIds`), `delivery.started` and `delivery.completed` (`orderId`, `orderIds`) match what the order service consumes.

