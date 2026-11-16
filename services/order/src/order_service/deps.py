"""FastAPI dependencies: database session, stock client, payment gateway."""

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from order_service.payments import MockPaymentGateway, PaymentGateway
from order_service.settings import OrderSettings, get_order_settings
from order_service.stock import HttpStockClient, StockClient


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(get_order_settings().database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def get_stock_client(settings: OrderSettings = Depends(get_order_settings)) -> StockClient:
    return HttpStockClient(settings.catalog_url)


def get_payment_gateway(settings: OrderSettings = Depends(get_order_settings)) -> PaymentGateway:
    return MockPaymentGateway(settings.mock_payment_fail_amount)
