# order service

**Owner:** Team 1

Cart, checkout saga, V1 and V2 orders, mock payments.

| | |
| --- | --- |
| Python package | `order_service` |
| Local dev port | 8006 |
| Database schema | `order` |

```bash
# from the repo root
uv run --package farm-order uvicorn order_service.main:app --reload --port 8006
# migrations (needs DATABASE_URL)
cd services/order && uv run alembic revision --autogenerate -m "describe change"
cd services/order && uv run alembic upgrade head
```

## Routes

| Route | Who | What |
| --- | --- | --- |
| `POST /orders/payments/intent` | customer | create a mock payment intent |
| `POST /orders` | customer | checkout (needs `Idempotency-Key`), one order per farm |
| `GET /orders` | customer, farmer, admin | own orders / farm orders / all; `status`, `role`, `cursor`, `limit` |
| `GET /orders/{id}` | customer, farmer, admin | one order with its status timeline |
| `POST /orders/{id}/pack` | farmer, admin | CONFIRMED to PACKED |
| `POST /orders/{id}/cancel` | customer, farmer (own farm), admin | cancel before dispatch, records a refund |
| `POST /orders/{id}/rating` | customer | rate a completed order |

JSON is camelCase. Events published and consumed are listed at the top of `src/order_service/events.py`.
Settings (env vars): `DATABASE_URL`, `RABBITMQ_URL`, `CATALOG_URL`, `DELIVERY_FEE_PER_ORDER`,
`MOCK_PAYMENT_FAIL_AMOUNT`, `EVENTS_ENABLED`.

```bash
uv run --package farm-order pytest services/order            # SQLite in memory
ORDER_TEST_DATABASE_URL=postgresql+asyncpg://farm:pw@localhost:5432/farm \
  uv run --package farm-order pytest services/order          # real Postgres (migrated first)
# after changing routes or models
uv run --package farm-order python services/order/scripts/export_openapi.py
uv run --package farm-order python services/order/scripts/export_events.py
```
