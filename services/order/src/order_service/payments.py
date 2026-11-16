"""Mock payment gateway. There is no real money: it approves everything except one test amount."""

from decimal import Decimal
from typing import Protocol


class PaymentGateway(Protocol):
    async def charge(self, intent_amount: Decimal, total: Decimal) -> bool: ...


class MockPaymentGateway:
    def __init__(self, fail_amount: Decimal | None = None) -> None:
        self.fail_amount = fail_amount

    async def charge(self, intent_amount: Decimal, total: Decimal) -> bool:
        return self.fail_amount is None or intent_amount != self.fail_amount
