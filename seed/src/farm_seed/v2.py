"""V2 seeding (land) and orders: plots with seasons, reservations taken to different stages with
crop updates, and produce orders. Needs V1 data first (run seed_v1 before this)."""

import time
from dataclasses import dataclass

from farm_seed.client import Gateway, SeedError
from farm_seed.data import PASSWORD, SEASONS, Dataset, V2Plan


def _pay_deposit(gw: Gateway, token: str, reservation_id: str, amount: object) -> None:
    """Order learns about an accepted reservation from the `reservation.accepted` event, so for a
    moment right after the accept it answers RESERVATION_NOT_FOUND: retry until it has caught up."""
    body = {"amount": amount, "kind": "DEPOSIT", "referenceId": reservation_id}
    deadline = time.monotonic() + 30
    while True:
        paid = gw.call(
            "POST", "/order/orders/payments/intent", token=token, json=body, ok=(200, 201, 404)
        )
        if paid.status_code != 404:
            return
        if time.monotonic() >= deadline:
            raise SeedError(f"order never saw reservation {reservation_id}: is RabbitMQ up?")
        time.sleep(0.5)


@dataclass
class V2Summary:
    plots: int = 0
    seasons: int = 0
    reservations: int = 0
    crop_updates: int = 0
    orders: int = 0


def _tokens(gw: Gateway, cache: dict[str, str], phone: str) -> str:
    if phone not in cache:
        cache[phone] = gw.login(phone, PASSWORD)["accessToken"]
    return cache[phone]


def seed_v2(gw: Gateway, data: Dataset, plan: V2Plan) -> V2Summary:
    summary = V2Summary()
    tokens: dict[str, str] = {}
    season_dates = {name: (start, end) for name, start, end in SEASONS}

    # --- plots + seasons: a farmer who already has plots is skipped, so re-runs add nothing ---
    plot_seasons: dict[tuple[int, int, str], str] = {}  # (farmer, plot, season) -> plot season id
    for f_idx, specs in plan.plots.items():
        farmer = data.farmers[f_idx]
        token = _tokens(gw, tokens, farmer.phone)
        mine = gw.call("GET", "/land/plots/mine", token=token).json()
        if mine:
            continue
        for p_idx, spec in enumerate(specs):
            created = gw.call(
                "POST",
                "/land/plots",
                token=token,
                json={
                    "title": spec.title,
                    "soilType": spec.soil_type,
                    "waterSource": spec.water_source,
                    "boundary": {"type": "Polygon", "coordinates": [spec.ring]},
                },
                ok=(201, 409),  # 409: overlaps another farmer's plot, rare; just skip it
            )
            if created.status_code != 201:
                continue
            plot = created.json()
            summary.plots += 1
            for name, (start, end) in season_dates.items():
                season = gw.call(
                    "POST",
                    f"/land/plots/{plot['id']}/seasons",
                    token=token,
                    json={
                        "season": name,
                        "startDate": start,
                        "endDate": end,
                        "price": spec.price_per_kg,
                        "allowedCrops": spec.crops,
                    },
                ).json()
                plot_seasons[(f_idx, p_idx, name)] = season["id"]
                summary.seasons += 1

    # --- reservations: only for plot seasons created in this run ---
    for res in plan.reservations:
        key = (res.farmer_index, res.plot_index, res.season)
        season_id = plot_seasons.get(key)
        if season_id is None:
            continue
        customer = data.customers[res.customer_index]
        c_token = _tokens(gw, tokens, customer.phone)
        f_token = _tokens(gw, tokens, data.farmers[res.farmer_index].phone)
        made = gw.call(
            "POST",
            "/land/reservations",
            token=c_token,
            json={"plotSeasonId": season_id, "cropId": res.crop_id, "expectedQtyKg": res.qty_kg},
        ).json()
        summary.reservations += 1
        if res.stage == "PENDING":
            continue
        accepted = gw.call("POST", f"/land/reservations/{made['id']}/accept", token=f_token).json()
        if res.stage == "ACCEPTED":
            continue
        _pay_deposit(gw, c_token, made["id"], accepted["deposit"])
        # land turns the reservation ACTIVE when it hears about the payment (an event), so crop
        # updates can be rejected for a moment: they are best effort
        for stage, note in res.updates:
            posted = gw.call(
                "POST",
                f"/land/reservations/{made['id']}/updates",
                token=f_token,
                json={"stage": stage, "note": note},
                ok=(201, 409, 422),
            )
            summary.crop_updates += 1 if posted.status_code == 201 else 0

    # --- produce orders ---
    listings: dict[int, list[dict]] = {}
    for n, order in enumerate(plan.orders):
        lines = []
        for f_idx, pick, qty in zip(order.farmer_indices, order.picks, order.qty, strict=True):
            if f_idx not in listings:
                token = _tokens(gw, tokens, data.farmers[f_idx].phone)
                page = gw.call("GET", "/catalog/listings/mine", token=token).json()
                listings[f_idx] = [li for li in page["items"] if li["status"] == "ACTIVE"]
            pool = listings[f_idx]
            if pool:
                lines.append({"listingId": pool[pick % len(pool)]["id"], "qty": qty})
        if not lines:
            continue
        customer = data.customers[order.customer_index]
        token = _tokens(gw, tokens, customer.phone)
        intent = gw.call(
            "POST", "/order/orders/payments/intent", token=token, json={"amount": 1, "kind": "FULL"}
        ).json()
        placed = gw.call(
            "POST",
            "/order/orders",
            token=token,
            idempotency_key=f"seed-order-{n:05d}",
            json={
                "items": lines,
                "deliveryAddress": {
                    "line1": f"{10 + n % 80} Main Road",
                    "city": "Seed City",
                    "pincode": "492001",
                    "location": {"type": "Point", "coordinates": [customer.lng, customer.lat]},
                },
                "paymentIntentId": intent["id"],
            },
            ok=(200, 201, 409, 422),
        )
        summary.orders += 1 if placed.status_code in (200, 201) else 0
    return summary
