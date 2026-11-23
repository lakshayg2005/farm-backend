"""Scripted API journeys V1 and V2 (HLD section 5): every step goes through the gateway and every state
change is asserted, so a broken hand-off between two services fails here instead of in the demo.

    V1  verified farmer -> listing -> order -> pack -> pooling -> delivery code -> COMPLETED -> rating
    V2  plot -> reservation -> deposit -> crop update -> harvest -> delivery (pooled) or buy-back

Each run creates its own people (phones carry a run id, names start with "Journey"), so it can run
against an empty stack in CI or against the demo data. The delivery partner pings its location at the
pickup first, and in offer dispatch (the default) accepts the route it is offered. On a shared stack,
run the journeys far from real partners (--near), so a real partner is never offered a test route:
with someone online within the offer radius, only they are offered it. User ids of everyone created
are written to --users-file, for loadtest/cleanup.py.

    uv run --package farm-seed python -m farm_seed.journeys --admin-phone ... --admin-password ... \
        [--near 22.08,82.15] [--users-file journey-users.txt]
"""

import argparse
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from farm_seed import photos
from farm_seed.client import Gateway
from farm_seed.data import PASSWORD
from farm_seed.v1 import _ensure_user, _point
from farm_seed.v2 import _pay_deposit

IST = ZoneInfo("Asia/Kolkata")


class JourneyError(AssertionError):
    pass


def today() -> date:
    return datetime.now(IST).date()


def step(text: str) -> None:
    print(f"  ok  {text}", flush=True)


def expect(condition: object, message: str) -> None:
    if not condition:
        raise JourneyError(message)


def wait_for(check: Callable[[], object], what: str, timeout: float = 90, interval: float = 1.0):
    """Polls until `check` returns something truthy (an event has to be handled first)."""
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() >= deadline:
            raise JourneyError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


class Run:
    """One journey run: the gateway, the admin, and people made fresh for this run."""

    def __init__(
        self,
        gw: Gateway,
        admin_phone: str,
        admin_password: str,
        near: tuple[float, float] | None = None,
        users_file: str | None = None,
    ):
        self.gw = gw
        self.near = near
        self.users_file = users_file
        self.admin = gw.login(admin_phone, admin_password)["accessToken"]
        self.id = f"{int(time.time()) % 10**6:06d}"
        self.rng = random.Random(self.id)
        self.crops = {c["id"]: c["defaultUnit"] for c in gw.call("GET", "/catalog/crops").json()}

    def phone(self, prefix: int, n: int) -> str:
        return f"+9199{prefix}{self.id}{n}"  # +91 and ten digits

    def person(
        self, label: str, prefix: int, n: int, roles: list[str], lat: float, lng: float
    ) -> dict:
        """Registers, has the admin verify a farmer, and signs in. Returns user, token, phone."""
        phone = self.phone(prefix, n)
        tokens = _ensure_user(self.gw, f"Journey {label}", phone, roles, lat, lng)
        user = tokens["user"]
        if self.users_file:
            with open(self.users_file, "a") as f:
                f.write(user["id"] + "\n")
        if "farmer" in roles:
            self.gw.call("POST", f"/identity/users/{user['id']}/verify-farmer", token=self.admin)
            tokens = self.gw.login(phone, PASSWORD)  # a new token says the farmer is verified
        return {"id": user["id"], "token": tokens["accessToken"], "phone": phone}

    def area(self, lat: float, lng: float) -> tuple[float, float]:
        """The journey's own default area, or around --near when given."""
        if self.near is None:
            return lat, lng
        return self.near[0] + (lat % 0.01), self.near[1] + (lng % 0.01)

    def get(self, path: str, token: str) -> dict | list:
        return self.gw.call("GET", path, token=token).json()

    def address(self, phone: str, lat: float, lng: float) -> dict:
        return {
            "line1": "12 Journey Lane",
            "city": "Durg",
            "pincode": "491001",
            "contactPhone": phone,
            "location": _point(lat, lng),
        }

    def photo(self, token: str, key: str, owner: str, owner_id: str, lat: float, lng: float) -> str:
        return photos.upload(self.gw, token, key, owner, owner_id, lat, lng, 0)

    def notified(self, user: dict, title: str) -> bool:
        rows = self.get("/notification/notifications?limit=50", user["token"])["items"]
        return any(n["title"] == title for n in rows)

    def deliver(self, order_id: str, customer: dict, partner: dict, others: dict) -> None:
        """Pool today's orders, then the partner collects and delivers this one with the code the
        customer reads out. Checks that only the customer sees the code."""
        gw, due = self.gw, today().isoformat()
        stops: list[dict] = []
        pools: list[dict] = []
        order = gw.call("GET", f"/order/orders/{order_id}", token=customer["token"]).json()
        hub = order["items"][0]["pickupLocation"]["coordinates"]  # [lng, lat]
        # The partner is on duty at the farm, so the offer goes to them (nearest online partner)
        gw.call(
            "POST",
            "/logistics/location",
            token=partner["token"],
            json={"lat": hub[1], "lng": hub[0], "accuracyM": 5},
        )

        def pooled() -> bool:
            gw.call("POST", f"/logistics/pools/run?pool_date={due}", token=self.admin)
            for pool in gw.call(
                "GET", f"/logistics/pools?pool_date={due}", token=self.admin
            ).json():
                if any(s["orderId"] == order_id for s in pool["stops"]):
                    expect(
                        pool.get("partnerId") in (None, partner["id"]),
                        "the route went to another delivery partner; run with --near far from "
                        "real partners",
                    )
                    stops[:] = [s for s in pool["stops"] if s["orderId"] == order_id]
                    pools[:] = [pool]
                    return True
            return False

        wait_for(pooled, "the pooling job to include the order", timeout=60, interval=3)
        if pools[0].get("partnerId") is None:  # offer dispatch: take the offered route
            pool_id = pools[0]["id"]
            offers = wait_for(
                lambda: [
                    o
                    for o in self.get("/logistics/offers/mine", partner["token"])
                    if (o.get("poolId") or o.get("pool_id")) == pool_id
                ],
                "the route offer to reach the partner",
                timeout=30,
            )
            expect(offers, "the partner was not offered the route")
            gw.call("POST", f"/logistics/offers/{pool_id}/accept", token=partner["token"])
            step("the route was offered to the nearby partner, who accepted it")
        expect(all(s["otp"] is None for s in stops), "the admin's pool list shows a delivery code")
        wait_for(
            lambda: self.get(f"/order/orders/{order_id}", customer["token"])["status"] == "POOLED",
            "the order to become POOLED",
        )
        track = self.get(f"/logistics/orders/{order_id}/track", customer["token"])
        code = (track.get("stop") or {}).get("otp")
        expect(code and len(code) == 4, f"the customer sees no delivery code: {track}")
        stranger = gw.call(
            "GET", f"/logistics/orders/{order_id}/track", token=others["token"], ok=(404,)
        )
        expect(stranger.status_code == 404, "another customer could track this order")
        route = self.get("/logistics/my-route", partner["token"])
        expect(all(s["otp"] is None for s in route["stops"]), "the partner sees the delivery code")

        for stop in (s for s in stops if s["stopType"] == "PICKUP"):
            gw.call(
                "POST", f"/logistics/stops/{stop['id']}/pickup", token=partner["token"], json={}
            )
        drop = next(s for s in stops if s["stopType"] == "DROP")
        expect(drop.get("customerPhone"), "the partner has no phone number for the customer")
        proof = self.photo(
            partner["token"], "delivery", "DELIVERY", drop["id"], drop["lat"], drop["lng"]
        )
        wrong = str((int(code) + 1) % 10_000).zfill(4)
        refused = gw.call(
            "POST",
            f"/logistics/stops/{drop['id']}/deliver",
            token=partner["token"],
            json={"photoMediaId": proof, "otp": wrong},
            ok=(422,),
        )
        expect(refused.json()["error"]["code"] == "INVALID_OTP", "a wrong code was accepted")
        gw.call(
            "POST",
            f"/logistics/stops/{drop['id']}/deliver",
            token=partner["token"],
            json={"photoMediaId": proof, "otp": code, "notes": "Journey test."},
        )
        wait_for(
            lambda: (
                self.get(f"/order/orders/{order_id}", customer["token"])["status"] == "COMPLETED"
            ),
            "the order to become COMPLETED",
            timeout=45,
        )


def v1(run: Run) -> None:
    """Customer buys produce: order, pack, pool, deliver with a code, complete, rate."""
    gw = run.gw
    lat, lng = run.area(21.19 + run.rng.random() * 0.01, 81.28 + run.rng.random() * 0.01)
    waiting = _ensure_user(gw, "Journey Unverified", run.phone(1, 9), ["farmer"], lat, lng)
    refused = gw.call(
        "POST",
        "/catalog/farms",
        token=waiting["accessToken"],
        json={"name": "Not yet", "district": "Durg", "location": _point(lat, lng)},
        ok=(403,),
    )
    expect(
        refused.json()["error"]["code"] == "FARMER_NOT_VERIFIED",
        "an unverified farmer could open a farm",
    )
    step("an unverified farmer cannot sell")
    farmer = run.person("Farmer V1", 1, 1, ["farmer"], lat, lng)
    customer = run.person("Customer V1", 2, 1, ["customer"], lat + 0.01, lng + 0.01)
    other = run.person("Other V1", 2, 9, ["customer"], lat + 0.02, lng)
    partner = run.person("Partner V1", 4, 1, ["delivery"], lat, lng)
    step("farmer verified by the admin, customers and a delivery partner registered")

    farm = gw.call(
        "POST",
        "/catalog/farms",
        token=farmer["token"],
        json={
            "name": f"Journey Farm {run.id}",
            "district": "Durg",
            "state": "Chhattisgarh",
            "location": _point(lat, lng),
        },
    ).json()
    listing = gw.call(
        "POST",
        "/catalog/listings",
        token=farmer["token"],
        json={
            "farmId": farm["id"],
            "cropId": "tomato",
            "variety": "Hybrid",
            "qtyAvailable": 100,
            "unit": run.crops["tomato"],
            "pricePerUnit": 30,
            "harvestDate": today().isoformat(),
            "isOrganic": False,
        },
    ).json()
    found = run.get(f"/catalog/listings/{listing['id']}", customer["token"])
    expect(found["qtyAvailable"] == 100, "the new listing is not readable by customers")
    step("listing created and visible to a customer")

    intent = gw.call(
        "POST",
        "/order/orders/payments/intent",
        token=customer["token"],
        json={"amount": 1, "kind": "FULL"},
    ).json()
    checkout = gw.call(
        "POST",
        "/order/orders",
        token=customer["token"],
        idempotency_key=f"journey-v1-{run.id}",
        json={
            "items": [{"listingId": listing["id"], "qty": 4}],
            "deliveryAddress": run.address(customer["phone"], lat + 0.01, lng + 0.01),
            "deliveryDate": today().isoformat(),
            "paymentIntentId": intent["id"],
        },
    ).json()
    order = checkout["orders"][0]
    expect(
        order["status"] == "CONFIRMED", f"a paid order should be CONFIRMED, got {order['status']}"
    )
    after = run.get(f"/catalog/listings/{listing['id']}", customer["token"])
    expect(after["qtyAvailable"] == 96, f"stock should drop to 96, is {after['qtyAvailable']}")
    step("order placed and paid: CONFIRMED, stock reduced")

    wait_for(
        lambda: run.notified(customer, "Order Placed Successfully"), "the customer's notification"
    )
    wait_for(
        lambda: run.notified(farmer, "New order to pack"), "the farmer's notification", timeout=30
    )
    step("customer and farmer were both notified")

    packed = gw.call("POST", f"/order/orders/{order['id']}/pack", token=farmer["token"]).json()
    expect(
        packed["status"] == "READY_FOR_DISPATCH",
        f"a packed order goes straight to READY_FOR_DISPATCH (no quality gate), got {packed['status']}",
    )
    step("farmer packed the order: READY_FOR_DISPATCH")

    run.deliver(order["id"], customer, partner, other)
    step("pooled, picked up, delivered with a photo and the customer's code: COMPLETED")

    gw.call(
        "POST",
        f"/order/orders/{order['id']}/rating",
        token=customer["token"],
        json={"score": 5, "comment": "Journey test."},
    )
    payouts = wait_for(
        lambda: [
            p
            for p in run.get("/order/orders/payouts?limit=50", farmer["token"])["items"]
            if p["orderId"] == order["id"]
        ],
        "the farmer's payout",
        timeout=30,
    )
    expect(payouts[0]["amount"] > 0, "payout amount should be positive")
    wait_for(
        lambda: run.notified(farmer, "Order delivered"),
        "the farmer's delivery notification",
        timeout=30,
    )
    step("rated; payout recorded and the farmer told")

    rated = wait_for(
        lambda: (lambda found: found if found["farmRatingCount"] == 1 else None)(
            run.get(f"/catalog/listings/{listing['id']}", customer["token"])
        ),
        "the farm rating to reach the listing",
        timeout=30,
    )
    expect(rated["farmRating"] == 5, f"farm rating should be 5, is {rated['farmRating']}")
    wait_for(
        lambda: run.notified(farmer, "New rating: 5 of 5"),
        "the farmer's rating notification",
        timeout=30,
    )
    step("the listing shows the farm's rating; the farmer was told")


def v2(run: Run) -> None:
    """Customer reserves land, follows the crop and takes the harvest by delivery or buy-back."""
    gw = run.gw
    lat, lng = run.area(22.1 + run.rng.random() * 0.4, 80.1 + run.rng.random() * 0.4)
    farmer = run.person("Farmer V2", 1, 2, ["farmer"], lat, lng)
    customer = run.person("Customer V2", 2, 2, ["customer"], lat + 0.01, lng)
    buyer = run.person("Customer V2b", 2, 3, ["customer"], lat + 0.01, lng)
    partner = run.person("Partner V2", 4, 2, ["delivery"], lat, lng)
    step("farmer verified, two customers and a delivery partner registered")

    seasons: list[tuple[str, dict]] = []
    for n in range(2):
        d_lat, d_lng = lat + n * 0.02, lng
        size = 0.0006
        ring = [
            [d_lng, d_lat],
            [d_lng + size, d_lat],
            [d_lng + size, d_lat + size],
            [d_lng, d_lat + size],
            [d_lng, d_lat],
        ]
        plot = gw.call(
            "POST",
            "/land/plots",
            token=farmer["token"],
            json={
                "title": f"Journey plot {run.id}-{n}",
                "soilType": "Black soil",
                "waterSource": "Borewell",
                "boundary": {"type": "Polygon", "coordinates": [ring]},
            },
        ).json()
        season = gw.call(
            "POST",
            f"/land/plots/{plot['id']}/seasons",
            token=farmer["token"],
            json={
                "season": "Rabi",
                "startDate": (today() + timedelta(days=1)).isoformat(),
                "endDate": (today() + timedelta(days=120)).isoformat(),
                "price": 25,
                "allowedCrops": ["rice"],
            },
        ).json()
        seasons.append((plot["id"], season))
    found = gw.call(
        "GET", f"/land/plots/search?lat={lat}&lng={lng}&radius_km=50", token=customer["token"]
    ).json()
    expect(found, "the new plots do not show in a nearby search")
    step("two plots with a season each, visible in a nearby search")

    def reserve(user: dict, season: dict) -> dict:
        made = gw.call(
            "POST",
            "/land/reservations",
            token=user["token"],
            json={"plotSeasonId": season["id"], "cropId": "rice", "expectedQtyKg": 600},
        ).json()
        expect(made["status"] == "PENDING", f"a new reservation is PENDING, got {made['status']}")
        wait_for(
            lambda: run.notified(farmer, "New land request"), "the farmer's land request notice"
        )
        accepted = gw.call(
            "POST", f"/land/reservations/{made['id']}/accept", token=farmer["token"]
        ).json()
        expect(accepted["status"] == "ACCEPTED", "the farmer's accept did not stick")
        _pay_deposit(gw, user["token"], made["id"], accepted["deposit"])
        wait_for(
            lambda: any(
                r["id"] == made["id"] and r["status"] == "ACTIVE"
                for r in run.get("/land/reservations/mine", user["token"])
            ),
            "the reservation to become ACTIVE after the deposit",
        )
        return made

    delivery = reserve(customer, seasons[0][1])
    buyback = reserve(buyer, seasons[1][1])
    step("both reservations accepted, deposit paid: ACTIVE")

    for made, user in ((delivery, customer), (buyback, buyer)):
        media = run.photo(farmer["token"], "stage_sown", "RESERVATION", made["id"], lat, lng)
        gw.call(
            "POST",
            f"/land/reservations/{made['id']}/updates",
            token=farmer["token"],
            json={
                "stage": "SOWN",
                "note": "Journey sowing.",
                "mediaIds": [media],
                "location": _point(lat, lng),
            },
        )
        updates = run.get(f"/land/reservations/{made['id']}/updates", user["token"])
        expect(
            len(updates) == 1 and updates[0]["mediaIds"] == [media],
            "customer cannot see the crop update",
        )
        wait_for(lambda u=user: run.notified(u, "Your rice: sown"), "the customer's crop notice")
    step("crop update with a photo posted by the farmer, read by the customer, who was told")

    for made in (delivery, buyback):
        gw.call(
            "POST",
            f"/land/reservations/{made['id']}/harvest-ready",
            token=farmer["token"],
            json={"harvestedQtyKg": 580},
        )

    def status_of(made: dict, user: dict) -> str:
        return next(
            r for r in run.get("/land/reservations/mine", user["token"]) if r["id"] == made["id"]
        )["status"]

    for made, user in ((delivery, customer), (buyback, buyer)):
        wait_for(
            lambda made=made, user=user: status_of(made, user) == "FULFILLING",
            "the reservation to wait for the customer's choice (FULFILLING)",
        )
    step("harvest marked ready with its weight: the harvest orders exist (no inspection)")

    def harvest_order(user: dict, body: dict) -> dict:
        def attempt():
            r = gw.call(
                "POST",
                "/order/orders/harvest",
                token=user["token"],
                json=body,
                ok=(200, 201, 404, 409, 422),
            )
            return r.json() if r.status_code in (200, 201) else None

        return wait_for(attempt, "the harvest order to be accepted")

    d = harvest_order(
        customer,
        {
            "reservationId": delivery["id"],
            "option": "DELIVERY",
            "deliveryAddress": run.address(customer["phone"], lat + 0.01, lng),
            "deliveryDate": today().isoformat(),
        },
    )
    b = harvest_order(buyer, {"reservationId": buyback["id"], "option": "BUYBACK"})
    for order in (d, b):
        got = run.get(
            f"/order/orders/{order['id']}", customer["token"] if order is d else buyer["token"]
        )
        expect(got["orderType"] == "HARVEST", f"expected a HARVEST order, got {got['orderType']}")
        expect(got["items"][0]["qty"] == 580, "the harvest order is not for the harvested weight")
    kinds = {p["kind"]: p["amount"] for p in b["payments"]}
    expect(
        kinds.get("BALANCE", 0) + kinds.get("DEPOSIT", 0) == b["subtotal"]
        and kinds.get("REFUND") == round(b["subtotal"] * 0.9, 2),
        f"buy-back must charge the balance, then credit 90%: {kinds}",
    )
    wait_for(lambda: status_of(buyback, buyer) == "COMPLETED", "the bought-back reservation")
    step("buy-back: balance charged, 90% credited, order and reservation COMPLETED")

    run.deliver(d["id"], customer, partner, buyer)
    wait_for(lambda: status_of(delivery, customer) == "COMPLETED", "the delivered reservation")
    step("the harvest went out on a pooled route with a delivery code: reservation COMPLETED")


JOURNEYS = {"v1": v1, "v2": v2}


def main() -> None:
    parser = argparse.ArgumentParser(prog="farm_seed.journeys", description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.getenv("SEED_BASE_URL", "http://localhost/api/v1"))
    parser.add_argument("--admin-phone", default=os.getenv("SEED_ADMIN_PHONE"))
    parser.add_argument("--admin-password", default=os.getenv("SEED_ADMIN_PASSWORD"))
    parser.add_argument("--journey", choices=("v1", "v2", "all"), default="all")
    parser.add_argument(
        "--near", help="lat,lng to run in, far from real delivery partners (shared stacks)"
    )
    parser.add_argument("--users-file", help="append the id of every user created here")
    args = parser.parse_args()
    near = tuple(float(x) for x in args.near.split(",")) if args.near else None
    if not (args.admin_phone and args.admin_password):
        parser.error(
            "give --admin-phone and --admin-password (or SEED_ADMIN_PHONE / SEED_ADMIN_PASSWORD)"
        )
    names = list(JOURNEYS) if args.journey == "all" else [args.journey]
    failed = False
    for name in names:
        print(f"{name.upper()}: {JOURNEYS[name].__doc__}", flush=True)
        started = datetime.now(UTC)
        try:
            JOURNEYS[name](
                Run(
                    Gateway(args.base_url),
                    args.admin_phone,
                    args.admin_password,
                    near,  # type: ignore[arg-type]
                    args.users_file,
                )
            )
        except (AssertionError, Exception) as exc:  # noqa: BLE001 - report every kind of failure
            failed = True
            print(f"FAIL {name.upper()}: {exc}", flush=True)
        else:
            print(f"PASS {name.upper()} in {(datetime.now(UTC) - started).seconds}s", flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
