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
