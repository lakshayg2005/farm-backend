"""Delivery modes: the fee of each, and when the shared-delivery batch runs (customer facing).

The batch itself is run by the logistics service (a cron at the same India time, see its
settings); this module only knows the schedule so the order can tell the customer when to expect
it.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from order_service.schemas import DeliveryMode, DeliveryOption, DeliveryOptions
from order_service.settings import OrderSettings

IST = ZoneInfo("Asia/Kolkata")


def fee_for(mode: DeliveryMode | str, settings: OrderSettings) -> Decimal:
    flexible = DeliveryMode(mode) == DeliveryMode.FLEXIBLE
    return settings.delivery_fee_flexible if flexible else settings.delivery_fee_per_order


def next_batch_at(
    settings: OrderSettings, delivery_date: date | None = None, now: datetime | None = None
) -> datetime:
    """The first batch run, at or after `now`, that may take an order due on `delivery_date`.

    The batch pools FLEXIBLE orders due that day or earlier, so an order due in three days
    waits for the batch of that day.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    day = max(delivery_date or now.date(), now.date())
    at = datetime.combine(day, time(settings.batch_hour_ist, settings.batch_minute_ist), tzinfo=IST)
    return at if at > now else at + timedelta(days=1)


def options(settings: OrderSettings, now: datetime | None = None) -> DeliveryOptions:
    at = next_batch_at(settings, None, now)
    clock = f"{settings.batch_hour_ist:02d}:{settings.batch_minute_ist:02d}"
    return DeliveryOptions(
        options=[
            DeliveryOption(
                mode=DeliveryMode.IMMEDIATE,
                title="Deliver as soon as possible",
                description="On a delivery route as soon as the farmer packs your order.",
                fee_per_order=float(fee_for(DeliveryMode.IMMEDIATE, settings)),
            ),
            DeliveryOption(
                mode=DeliveryMode.FLEXIBLE,
                title="I can wait: shared delivery",
                description=(
                    f"Your order joins our daily {clock} batch, where orders from nearby homes "
                    "share one delivery partner. It arrives later, and costs less."
                ),
                fee_per_order=float(fee_for(DeliveryMode.FLEXIBLE, settings)),
            ),
        ],
        batch_time=clock,
        next_batch_at=at,
    )
