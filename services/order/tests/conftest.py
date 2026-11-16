"""Test setup: an in-memory SQLite database that uses the real models (the `order` schema is an
attached database), a fake catalog, the mock payment gateway and a switchable logged-in user."""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from farm_common.auth import CurrentUser, Role, get_current_user
from farm_common.errors import AppError
from order_service.db import Base, OutboxEvent
from order_service.deps import get_payment_gateway, get_session, get_stock_client
from order_service.main import app
from order_service.payments import MockPaymentGateway
from order_service.settings import OrderSettings, get_order_settings
from order_service.stock import ReservedLine, StockRequestItem

CUSTOMER = UUID("7c1d9f0a-2b34-4e6f-8a90-1b2c3d4e5f60")
OTHER_CUSTOMER = UUID("8d2e0a1b-3c45-4f70-9b01-2c3d4e5f6071")
FARMER_A = UUID("11111111-1111-4111-8111-111111111111")
FARMER_B = UUID("22222222-2222-4222-8222-222222222222")
FARM_A = UUID("c3d2ebfa-9a8d-4c7e-bf60-5b4c3d2e1f0a")
FARM_B = UUID("d4c3fc0b-ab9e-4d8f-c071-6c5d4e3f2a1b")
TOMATO = UUID("e5b40d1c-bcaf-4e90-d182-7d6e5f4a3b2c")
ONION = UUID("a7c62f3e-dec1-4ab2-f3a4-9f8a7b6c5d4e")
SPINACH = UUID("f6a51e2d-cdb0-4fa1-e293-8e7f6a5b4c3d")
FAIL_AMOUNT = Decimal("13.13")
PICKUP = {"type": "Point", "coordinates": [75.8, 26.85]}  # a farm, GeoJSON [lng, lat]


def user(user_id: UUID, *roles: Role) -> CurrentUser:
    return CurrentUser(id=str(user_id), roles=list(roles))


class FakeStock:
    """Stands in for catalog: GET /listings/{id} (farm lookup) and /internal/stock/*."""

    def __init__(self) -> None:
        # listing -> [farm, owner, crop name, unit, price, available]
        self.listings = {
            TOMATO: [FARM_A, FARMER_A, "Tomato", "kg", Decimal("40.00"), Decimal(100)],
            ONION: [FARM_A, FARMER_A, "Onion", "kg", Decimal("30.00"), Decimal(100)],
            SPINACH: [FARM_B, FARMER_B, "Spinach", "kg", Decimal("25.50"), Decimal(100)],
        }
        self.reserved: dict[UUID, list[StockRequestItem]] = {}
        self.released: list[UUID] = []

    def available(self, listing: UUID) -> Decimal:
        return self.listings[listing][5]

    async def farms_of(self, listing_ids):
        missing = [i for i in listing_ids if i not in self.listings]
        if missing:
            raise AppError("LISTING_NOT_FOUND", "A listing in the cart does not exist", 404)
        return {i: self.listings[i][0] for i in listing_ids}

    async def reserve(self, order_id, items):
        short = [
            {
                "listingId": str(i.listing_id),
                "requested": float(i.qty),
                "available": float(self.listings[i.listing_id][5]),
            }
            for i in items
            if i.qty > self.listings[i.listing_id][5]
        ]
        if short:
            raise AppError("STOCK_UNAVAILABLE", "Not enough stock", 409, details=short)
        lines = []
        for item in items:
            farm, owner, name, unit, price, _ = self.listings[item.listing_id]
            self.listings[item.listing_id][5] -= item.qty
            lines.append(
                ReservedLine(item.listing_id, farm, owner, name, unit, price, item.qty, PICKUP)
            )
        self.reserved[order_id] = items
        return lines

    async def release(self, order_id):
        self.released.append(order_id)
        for item in self.reserved.pop(order_id, []):
            self.listings[item.listing_id][5] += item.qty


@dataclass
class Session:
    """What a test gets: an HTTP client plus direct access to the fakes and the database."""

    http: AsyncClient
    stock: FakeStock
    session_factory: async_sessionmaker[AsyncSession]
    current: CurrentUser = field(default_factory=lambda: user(CUSTOMER, Role.CUSTOMER))

    def login(self, who: CurrentUser) -> None:
        self.current = who

    async def outbox(self, event_type: str | None = None) -> list[OutboxEvent]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(select(OutboxEvent).order_by(OutboxEvent.occurred_at))
            ).scalars()
            return [r for r in rows if event_type is None or r.type == event_type]

    async def intent(self, amount: float = 100) -> str:
        response = await self.http.post("/orders/payments/intent", json={"amount": amount})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def checkout(self, items=None, key: str = "checkout-0001", amount: float = 100, **extra):
        items = items or [
            {"listingId": str(TOMATO), "qty": 2},
            {"listingId": str(SPINACH), "qty": 1},
        ]
        body = {
            "items": items,
            "deliveryAddress": {
                "line1": "12 Green Park",
                "city": "Jaipur",
                "pincode": "302001",
                "location": {"type": "Point", "coordinates": [75.7873, 26.9124]},
            },
            "paymentIntentId": extra.pop("payment_intent_id", None) or await self.intent(amount),
            **extra,
        }
        return await self.http.post("/orders", json=body, headers={"Idempotency-Key": key})

    async def placed_order(self, farm: UUID = FARM_A) -> dict:
        """Checkout as the default customer and return the order of `farm`."""
        response = await self.checkout()
        assert response.status_code == 201, response.text
        return next(o for o in response.json()["orders"] if o["farmId"] == str(farm))


@pytest.fixture
async def session_factory():
    """SQLite in memory by default. Set ORDER_TEST_DATABASE_URL to a migrated Postgres (for example
    postgresql+asyncpg://farm:pw@localhost:5432/farm) to run the same tests on the real thing;
    its order tables are emptied before every test."""
    url = os.environ.get("ORDER_TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            tables = ", ".join(f'"{t.schema}"."{t.name}"' for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

        @event.listens_for(engine.sync_engine, "connect")
        def attach_schema(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute('ATTACH DATABASE ":memory:" AS "order"')
            cursor.close()

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def api(session_factory):
    stock = FakeStock()
    state = Session(
        http=AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        stock=stock,
        session_factory=session_factory,
    )

    async def session_dependency():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    app.dependency_overrides[get_stock_client] = lambda: stock
    app.dependency_overrides[get_payment_gateway] = lambda: MockPaymentGateway(FAIL_AMOUNT)
    app.dependency_overrides[get_order_settings] = lambda: OrderSettings(
        delivery_fee_per_order=Decimal("20.00"), mock_payment_fail_amount=FAIL_AMOUNT
    )
    app.dependency_overrides[get_current_user] = lambda: state.current
    yield state
    app.dependency_overrides.clear()
    await state.http.aclose()
