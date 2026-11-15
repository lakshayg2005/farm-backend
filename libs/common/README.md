# libs/common (`farm-common`)

Shared Python package used by every service. Import it as `farm_common`. Don't copy this code into a
service; if something is missing, add it here (both team leads review changes).

| Module | What it gives you |
| --- | --- |
| `farm_common.app` | `create_app(name, version)`: FastAPI app with `/health`, correlation IDs, JSON logs, standard errors |
| `farm_common.settings` | `ServiceSettings` / `get_settings()`: DB, RabbitMQ, Redis URLs, JWT key, `root_path` from env vars |
| `farm_common.auth` | `get_current_user`, `require_roles(Role.FARMER, ...)`, `CurrentUser`, `Role` |
| `farm_common.errors` | `AppError(code, message, status)`, `NotFound`, `Conflict`, `Unauthorized`, `Forbidden` |
| `farm_common.log` | `X-Correlation-ID` middleware, `get_correlation_id()`, JSON log formatter |
| `farm_common.events` | `Event` envelope, `RabbitEventBus` (publish/subscribe with retries + dead-letter queue), `InMemoryEventBus` for tests |
| `farm_common.schemas` | `ApiModel`: base for request/response bodies, camelCase JSON (`homeLat`) and snake_case Python |
| `farm_common.outbox` | `OutboxMixin`, `ProcessedEventMixin`, `add_event`, `run_relay`, `mark_processed` |

## Protecting a route

```python
from fastapi import Depends
from farm_common.auth import CurrentUser, Role, require_roles


@router.post("/listings")
async def create_listing(
    body: ListingIn, user: CurrentUser = Depends(require_roles(Role.FARMER))
): ...
```

Missing or bad tokens get `401` (`UNAUTHORIZED`, `INVALID_TOKEN`, `TOKEN_EXPIRED`), wrong role gets `403`.
Set `JWT_PUBLIC_KEY_PATH` (or `JWT_PUBLIC_KEY`) to the identity service's public key.

## Errors

```python
from farm_common.errors import AppError, NotFound

raise AppError("STOCK_UNAVAILABLE", "Only 3 kg left", status_code=409)
raise NotFound("Listing not found")
```

Every error, including validation errors and crashes, comes back as:

```json
{"error": {"code": "STOCK_UNAVAILABLE", "message": "Only 3 kg left", "correlationId": "4f1c..."}}
```

## Publishing events (always through the outbox)

```python
# db.py
class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"


# in a route or service function, in the SAME transaction as the state change
order.status = "PLACED"
add_event(
    session,
    OutboxEvent,
    Event(type="order.placed", producer="order", payload={"orderId": str(order.id)}),
)
await session.commit()

# on startup
bus = RabbitEventBus(settings.rabbitmq_url, service_name="order")
await bus.connect()
asyncio.create_task(run_relay(session_factory, OutboxEvent, bus))
```

## Consuming events (idempotently)

```python
async def on_quality_graded(event: Event) -> None:
    async with session_factory() as session, session.begin():
        if not await mark_processed(session, ProcessedEvent, event.event_id):
            return  # duplicate delivery
        ...apply the change...

await bus.subscribe(["quality.graded"], on_quality_graded)
```

A handler that raises is retried 3 times, then the message moves to `<service>.main.dlq`.
Event names and payloads are documented as JSON Schemas in `contracts/events/`.
