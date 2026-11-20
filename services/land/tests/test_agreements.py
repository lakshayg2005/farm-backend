from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from land_service import routes as land_routes
from land_service.agreements import build_terms
from land_service.db import Agreement, Plot, PlotSeason, Reservation


def test_terms_split_price_into_deposit_and_balance():
    agreed = build_terms(
        plot_title="Riverside plot",
        area_acres=2.5,
        season="Kharif",
        start_date=date(2026, 6, 15),
        end_date=date(2026, 10, 31),
        crop_id="green-chilli",
        expected_qty_kg=400,
        price_per_kg=25,
        deposit_rate=0.2,
    )
    assert (agreed.total, agreed.deposit, agreed.balance) == (10000, 2000, 8000)
    text = " ".join(agreed.terms)
    assert "Riverside plot" in text and "Kharif" in text and "green chilli" in text
    assert "₹2,000" in text and "₹8,000" in text and "20%" in text


@pytest.mark.asyncio
async def test_accepting_writes_an_agreement_and_mentions_it_in_the_event(monkeypatch):
    farmer_id, customer_id = uuid4(), uuid4()
    plot = SimpleNamespace(id=uuid4(), title="Riverside plot", area_acres=2.5)
    season = SimpleNamespace(
        id=uuid4(),
        plot_id=plot.id,
        season="Rabi",
        start_date=date(2026, 11, 1),
        end_date=date(2027, 3, 31),
        price=30,
    )
    reservation = SimpleNamespace(
        id=uuid4(),
        plot_season_id=season.id,
        customer_id=customer_id,
        farmer_id=farmer_id,
        crop_id="wheat",
        expected_qty_kg=100,
        deposit=None,
        total=None,
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    objects = {Reservation: reservation, PlotSeason: season, Plot: plot}
    session = MagicMock()
    session.get = AsyncMock(side_effect=lambda model, _id: objects[model])
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    published = []
    monkeypatch.setattr(land_routes, "add_event", lambda _s, _m, event: published.append(event))
    monkeypatch.setattr(land_routes, "reservation_out", lambda row: row)
    user = SimpleNamespace(id=str(farmer_id), has_any=lambda *_: False)

    await land_routes.accept_reservation(reservation.id, user, session)

    agreement = session.add.call_args.args[0]
    assert isinstance(agreement, Agreement)
    assert reservation.status == "ACCEPTED"
    assert (agreement.total, agreement.deposit, agreement.balance) == (3000, 600, 2400)
    assert reservation.deposit == 600
    assert published[0].payload["agreementId"] == str(agreement.id)
