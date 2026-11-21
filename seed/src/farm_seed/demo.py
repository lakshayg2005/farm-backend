"""The curated demo: a small, believable story across every part of the platform, built only through the
public API (so it passes the same validation and publishes the same events as real traffic).

Run it on an empty database: the order lifecycle steps pick up orders that are still CONFIRMED and the
pooling job groups whatever is ready, so a second run on top of old data would mix the two.
"""

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from farm_seed import demo_data as D
from farm_seed import photos
from farm_seed.client import Gateway, SeedError
from farm_seed.data import PASSWORD, _square
from farm_seed.ops import _wait
from farm_seed.v1 import _ensure_user, _point
from farm_seed.v2 import _pay_deposit

IST = ZoneInfo("Asia/Kolkata")


def today() -> date:
    return datetime.now(IST).date()


# Photos taken far from their farm, so the "unverified" badge has something to show.
FAR_AWAY = {("Verma Organic Farm", "cabbage")}

# Orders: (customer index, ((farmer index, crop, qty), ...), fate). A fate is what happens next:
#   CONFIRMED   left for the farmer to pack          CANCELLED     the customer cancels
#   WAITING     packed, inspection still open        FAIL_LOWER    inspection failed, lower grade accepted
#   DISPATCH    inspected and passed, then pooled into routes
ORDERS = (
    (0, ((0, "tomato", 4), (1, "cauliflower", 2)), "DISPATCH"),
    (1, ((0, "tomato", 3), (0, "brinjal", 2)), "DISPATCH"),
    (2, ((2, "onion", 5), (2, "potato", 5)), "DISPATCH"),
    (3, ((1, "carrot", 2), (1, "peas", 1)), "DISPATCH"),
    (4, ((3, "guava", 3), (3, "banana", 2)), "DISPATCH"),
    (5, ((5, "spinach", 4), (5, "okra", 2)), "DISPATCH"),
    (6, ((0, "bottle-gourd", 3), (0, "tomato", 2)), "DISPATCH"),
    (7, ((1, "cabbage", 3), (1, "cauliflower", 1)), "DISPATCH"),
    (0, ((2, "pumpkin", 1), (2, "onion", 3)), "DISPATCH"),
    (1, ((5, "peas", 2),), "DISPATCH"),
    (4, ((0, "brinjal", 3),), "DISPATCH"),
    (8, ((3, "guava", 4),), "DISPATCH"),
    (9, ((1, "carrot", 3), (1, "peas", 2)), "DISPATCH"),
    (3, ((2, "potato", 6),), "DISPATCH"),
    (2, ((4, "chana", 1),), "FAIL_LOWER"),
    (5, ((0, "tomato", 5),), "WAITING"),
    (6, ((5, "okra", 3),), "WAITING"),
    (7, ((2, "onion", 4),), "WAITING"),
    (0, ((5, "spinach", 6), (5, "peas", 1)), "CONFIRMED"),
    (3, ((0, "bottle-gourd", 2),), "CONFIRMED"),
    (6, ((1, "cabbage", 2),), "CONFIRMED"),
    (8, ((2, "potato", 4),), "CONFIRMED"),
    (9, ((3, "banana", 1),), "CONFIRMED"),
    (4, ((5, "peas", 2),), "CANCELLED"),
    (7, ((0, "tomato", 2),), "CANCELLED"),
)
REVIEWS = (
    (5, "Fresh and exactly as described. Delivered on time."),
    (4, "Good quality vegetables, packed well."),
    (5, "Everything was fresh. Will order again."),
    (4, "Tasty produce. The delivery was a little late."),
)


@dataclass
class Summary:
    farmers: int = 0
    unverified_farmers: int = 0
    customers: int = 0
    farms: int = 0
    listings: int = 0
    photos: int = 0
    plots: int = 0
    reservations: int = 0
    crop_updates: int = 0
    orders: int = 0
    inspections_reported: int = 0
    pools: int = 0
    delivered_orders: int = 0
    ratings: int = 0
    notes: list[str] = field(default_factory=list)


class Demo:
    def __init__(self, gw: Gateway, admin_phone: str, admin_password: str):
        self.gw = gw
        self.admin = gw.login(admin_phone, admin_password)["accessToken"]
        self.s = Summary()
        self.tokens: dict[str, str] = {}
        self.farmer_ids: list[str] = []
        self.customer_ids: list[str] = []
        self.farm_ids: dict[int, str] = {}
        self.farm_points: dict[int, tuple[float, float]] = {}
        self.listings: dict[tuple[int, str], str] = {}
        self.plot_ids: dict[int, str] = {}
        self.season_ids: dict[tuple[int, str], str] = {}
        self.inspector_tokens: dict[str, str] = {}
        self.partner_phone: dict[str, str] = {}
        self.units = {c["id"]: c["defaultUnit"] for c in gw.call("GET", "/catalog/crops").json()}

    # --- small helpers -------------------------------------------------------------------------
    def token(self, phone: str) -> str:
        if phone not in self.tokens:
            self.tokens[phone] = self.gw.login(phone, PASSWORD)["accessToken"]
        return self.tokens[phone]

    def photo(
        self, phone: str, key: str, owner: str, owner_id: str, lat: float, lng: float, days: float
    ) -> str:
        self.s.photos += 1
        return photos.upload(self.gw, self.token(phone), key, owner, owner_id, lat, lng, days)

    # --- people ---------------------------------------------------------------------------------
    def people(self) -> None:
        gw = self.gw
        for farmer in D.FARMERS:
            tokens = _ensure_user(gw, farmer.name, farmer.phone, ["farmer"])
            user = tokens["user"]
            if farmer.verified and not user.get("farmerVerified"):
                gw.call("POST", f"/identity/users/{user['id']}/verify-farmer", token=self.admin)
            self.farmer_ids.append(user["id"])
            self.s.farmers += 1 if farmer.verified else 0
            self.s.unverified_farmers += 0 if farmer.verified else 1
        for c in D.CUSTOMERS:
            user = _ensure_user(gw, c.name, c.phone, ["customer"], c.lat, c.lng)["user"]
            self.customer_ids.append(user["id"])
            self.s.customers += 1
        for name, phone, lat, lng in D.INSPECTORS:
            user = _ensure_user(gw, name, phone, ["customer"], lat, lng)["user"]
            if "inspector" not in user.get("roles", []):
                gw.call(
                    "PUT",
                    f"/identity/users/{user['id']}/roles",
                    token=self.admin,
                    json={"roles": ["inspector"]},
                )
            self.tokens.pop(phone, None)  # roles changed: old tokens are revoked
            token = self.token(phone)
            gw.call(
                "PUT",
                "/quality/inspectors/me/location",
                token=token,
                json={"location": _point(lat, lng), "available": True},
            )
            self.inspector_tokens[user["id"]] = token
        for name, phone in D.PARTNERS:
            partner = _ensure_user(gw, name, phone, ["delivery"])["user"]
            self.partner_phone[partner["id"]] = phone

    # --- farms and listings ----------------------------------------------------------------------
    def catalog(self) -> None:
        gw = self.gw
        today_ = today()
        for f_idx, farmer in enumerate(D.FARMERS):
            token = self.token(farmer.phone)
            for farm in farmer.farms:
                made = gw.call(
                    "POST",
                    "/catalog/farms",
                    token=token,
                    idempotency_key=f"demo-farm-{f_idx}",
                    json={
                        "name": farm.name,
                        "village": farm.village,
                        "district": D.DISTRICT,
                        "state": D.STATE,
                        "location": _point(farm.lat, farm.lng),
                    },
                ).json()
                self.farm_ids[f_idx] = made["id"]
                self.farm_points[f_idx] = (farm.lat, farm.lng)
                self.s.farms += 1
                self.photo(
                    farmer.phone,
                    farm.photo,
                    "FARM",
                    made["id"],
                    farm.lat + 0.0006,
                    farm.lng + 0.0005,
                    30,
                )
                for j, item in enumerate(farm.listings):
                    listing = gw.call(
                        "POST",
                        "/catalog/listings",
                        token=token,
                        idempotency_key=f"demo-listing-{f_idx}-{j}",
                        json={
                            "farmId": made["id"],
                            "cropId": item.crop,
                            "variety": item.variety,
                            "description": item.description,
                            "qtyAvailable": item.qty,
                            "unit": self.units[item.crop],
                            "pricePerUnit": item.price,
                            "harvestDate": (
                                today_ - timedelta(days=item.harvest_days_ago)
                            ).isoformat(),
                            "isOrganic": item.organic,
                        },
                    ).json()
                    self.listings[(f_idx, item.crop)] = listing["id"]
                    self.s.listings += 1
                    if item.photo:
                        far = (farm.name, item.crop) in FAR_AWAY
                        lat = farm.lat + (0.055 if far else 0.0008)
                        media = self.photo(
                            farmer.phone,
                            item.photo,
                            "LISTING",
                            listing["id"],
                            lat,
                            farm.lng + 0.0004,
                            item.harvest_days_ago,
                        )
                        gw.call(
                            "PATCH",
                            f"/catalog/listings/{listing['id']}",
                            token=token,
                            json={"mediaIds": [media]},
                        )

    # --- land and reservations -----------------------------------------------------------------
    def land(self) -> None:
        gw = self.gw
        for p_idx, spec in enumerate(D.PLOTS):
            farmer = D.FARMERS[spec.farmer]
            token = self.token(farmer.phone)
            plot = gw.call(
                "POST",
                "/land/plots",
                token=token,
                json={
                    "title": spec.title,
                    "description": f"{spec.soil}, {spec.water.lower()} irrigation.",
                    "soilType": spec.soil,
                    "waterSource": spec.water,
                    "boundary": {
                        "type": "Polygon",
                        "coordinates": [_square(spec.lat, spec.lng, spec.side_m)],
                    },
                },
            ).json()
            self.plot_ids[p_idx] = plot["id"]
            self.s.plots += 1
            seasons = tuple(
                D.PAST_KHARIF
                if (n == "Kharif" and p_idx in D.PLOTS_WITH_PAST_KHARIF)
                else (n, a, b)
                for n, a, b in D.SEASONS
            )
            for name, start, end in seasons:
                season = gw.call(
                    "POST",
                    f"/land/plots/{plot['id']}/seasons",
                    token=token,
                    json={
                        "season": name,
                        "startDate": start,
                        "endDate": end,
                        "price": spec.price_per_kg,
                        "allowedCrops": list(spec.crops),
                    },
                ).json()
                self.season_ids[(p_idx, name)] = season["id"]

    def _active(self, customer_phone: str, reservation_id: str) -> bool:
        rows = self.gw.call(
            "GET", "/land/reservations/mine", token=self.token(customer_phone)
        ).json()
        return any(r["id"] == reservation_id and r["status"] == "ACTIVE" for r in rows)

    def _address(self, c: D.Customer) -> dict:
        return {
            "line1": c.area,
            "city": "Bhilai"
            if "Bhilai" in c.area
            else ("Raipur" if "Raipur" in c.area else "Durg"),
            "pincode": "492001",
            "contactPhone": c.phone,
            "location": {"type": "Point", "coordinates": [c.lng, c.lat]},
        }

    def reservations(self) -> dict[str, tuple[D.Customer, D.PlotSpec]]:
        """Takes each reservation to its stage. Returns the harvested ones (they continue in finish_harvests)."""
        gw = self.gw
        harvested: dict[str, tuple[D.Customer, D.PlotSpec]] = {}
        for r in D.RESERVATIONS:
            spec = D.PLOTS[r.plot]
            farmer_phone = D.FARMERS[spec.farmer].phone
            customer = D.CUSTOMERS[r.customer]
            c_token, f_token = self.token(customer.phone), self.token(farmer_phone)
            made = gw.call(
                "POST",
                "/land/reservations",
                token=c_token,
                json={
                    "plotSeasonId": self.season_ids[(r.plot, r.season)],
                    "cropId": r.crop,
                    "expectedQtyKg": r.qty_kg,
                },
            ).json()
            self.s.reservations += 1
            rid = made["id"]
            if r.stage == "PENDING":
                continue
            if r.stage == "REJECTED":
                gw.call("POST", f"/land/reservations/{rid}/reject", token=f_token, json={})
                continue
            if r.stage == "CANCELLED":
                gw.call("POST", f"/land/reservations/{rid}/cancel", token=c_token, json={})
                continue
            accepted = gw.call("POST", f"/land/reservations/{rid}/accept", token=f_token).json()
            if r.stage == "ACCEPTED":
                continue
            _pay_deposit(gw, c_token, rid, accepted["deposit"])
            if not _wait(partial(self._active, customer.phone, rid), 60):
                self.s.notes.append(f"reservation {rid} never became ACTIVE (is RabbitMQ up?)")
                continue
            for stage, note, key, days in r.updates:
                media = self.photo(
                    farmer_phone,
                    key,
                    "RESERVATION",
                    rid,
                    spec.lat + 0.0004,
                    spec.lng + 0.0003,
                    days,
                )
                gw.call(
                    "POST",
                    f"/land/reservations/{rid}/updates",
                    token=f_token,
                    json={
                        "stage": stage,
                        "note": note,
                        "mediaIds": [media],
                        "location": _point(spec.lat, spec.lng),
                    },
                )
                self.s.crop_updates += 1
            if r.stage in ("HARVEST_READY", "HARVESTED"):
                gw.call("POST", f"/land/reservations/{rid}/harvest-ready", token=f_token)
            if r.stage == "HARVESTED":
                harvested[rid] = (customer, spec)
        return harvested

    def finish_harvests(self, harvested: dict[str, tuple[D.Customer, D.PlotSpec]]) -> None:
        """Inspector passes the harvest, then the customer chooses delivery: it joins the pooled routes."""
        gw = self.gw
        for rid, (customer, plot) in harvested.items():
            found: dict = {}

            def has_inspection(rid: str = rid, found: dict = found) -> bool:
                rows = gw.call("GET", "/quality/inspections", token=self.admin).json()
                match = [x for x in rows if x["subjectType"] == "HARVEST" and x["subjectId"] == rid]
                found.update(match[0] if match else {})
                return bool(match)

            if not _wait(has_inspection, 60):
                self.s.notes.append(f"harvest {rid} got no inspection")
                continue
            token = self.inspector_tokens.get(found.get("inspectorId") or "", self.admin)
            media = self.staff_photo(
                token, "inspection", "INSPECTION", found["id"], plot.lat, plot.lng, 3
            )
            gw.call(
                "POST",
                f"/quality/inspections/{found['id']}/report",
                token=token,
                json={
                    "grade": "A",
                    "moisturePct": 12.0,
                    "pestFound": False,
                    "weightKg": 780,
                    "result": "PASS",
                    "note": "Clean, well dried grain. Weight matches the reservation.",
                    "mediaIds": [media],
                },
            )
            self.s.inspections_reported += 1
            deadline = time.monotonic() + 60
            while True:
                made = self.gw.call(
                    "POST",
                    "/order/orders/harvest",
                    token=self.token(customer.phone),
                    json={
                        "reservationId": rid,
                        "option": "DELIVERY",
                        "deliveryAddress": self._address(customer),
                        "deliveryDate": today().isoformat(),
                    },
                    ok=(200, 201, 404, 409, 422),
                )
                if made.status_code in (200, 201):
                    self.s.orders += 1
                    break
                if time.monotonic() > deadline:
                    self.s.notes.append(
                        f"harvest order for {rid} was not created: {made.text[:120]}"
                    )
                    break
                time.sleep(1)

    def staff_photo(
        self, token: str, key: str, owner: str, owner_id: str, lat: float, lng: float, days: float
    ) -> str:
        self.s.photos += 1
        return photos.upload(self.gw, token, key, owner, owner_id, lat, lng, days)

    # --- orders ---------------------------------------------------------------------------------
    def orders(self) -> list[dict]:
        """Places every curated order. Returns one record per per-farm order."""
        gw = self.gw
        farm_to_farmer = {farm_id: i for i, farm_id in self.farm_ids.items()}
        placed: list[dict] = []
        for n, (c_idx, lines, fate) in enumerate(ORDERS):
            customer = D.CUSTOMERS[c_idx]
            token = self.token(customer.phone)
            intent = gw.call(
                "POST",
                "/order/orders/payments/intent",
                token=token,
                json={"amount": 1, "kind": "FULL"},
            ).json()
            checkout = gw.call(
                "POST",
                "/order/orders",
                token=token,
                idempotency_key=f"demo-order-{n:03d}",
                json={
                    "items": [
                        {"listingId": self.listings[(f, crop)], "qty": qty}
                        for f, crop, qty in lines
                    ],
                    "deliveryAddress": self._address(customer),
                    "deliveryDate": today().isoformat(),
                    "paymentIntentId": intent["id"],
                },
            ).json()
            for order in checkout["orders"]:
                placed.append(
                    {
                        "id": order["id"],
                        "fate": fate,
                        "farmer": farm_to_farmer[order["farmId"]],
                        "customer": c_idx,
                    }
                )
                self.s.orders += 1
        return placed

    def lifecycle(self, placed: list[dict]) -> None:
        """Pack, cancel and inspect so every queue and every status has something in it."""
        gw = self.gw
        packed: list[dict] = []
        for o in placed:
            if o["fate"] == "CONFIRMED":
                continue
            if o["fate"] == "CANCELLED":
                gw.call(
                    "POST",
                    f"/order/orders/{o['id']}/cancel",
                    token=self.token(D.CUSTOMERS[o["customer"]].phone),
                    json={},
                )
                continue
            gw.call(
                "POST",
                f"/order/orders/{o['id']}/pack",
                token=self.token(D.FARMERS[o["farmer"]].phone),
            )
            packed.append(o)

        def inspections() -> dict[str, dict]:
            rows = gw.call("GET", "/quality/inspections", token=self.admin).json()
            return {r["subjectId"]: r for r in rows if r["subjectType"] == "ORDER"}

        wanted = {o["id"] for o in packed}
        if not _wait(lambda: wanted <= inspections().keys(), 90):
            self.s.notes.append(
                "not every packed order got an inspection in time (is RabbitMQ up?)"
            )
        found = inspections()
        for o in packed:
            inspection = found.get(o["id"])
            if inspection is None or o["fate"] == "WAITING" or inspection["status"] != "PENDING":
                continue
            token = self.inspector_tokens.get(inspection.get("inspectorId") or "", self.admin)
            lat, lng = self.farm_points[o["farmer"]]
            media = self.staff_photo(
                token, "inspection", "INSPECTION", inspection["id"], lat + 0.0003, lng, 0
            )
            report = {
                "grade": "A",
                "moisturePct": 11.5,
                "pestFound": False,
                "weightKg": 20,
                "result": "PASS",
                "note": "Fresh, clean and correctly packed.",
                "mediaIds": [media],
            }
            if o["fate"] == "FAIL_LOWER":
                report.update(
                    result="FAIL",
                    grade="B",
                    note="Some grains are damaged. The buyer accepted a lower grade.",
                    resolution="ACCEPT_LOWER_GRADE",
                    acceptedGrade="C",
                )
            gw.call(
                "POST", f"/quality/inspections/{inspection['id']}/report", token=token, json=report
            )
            self.s.inspections_reported += 1

    def deliveries(self, placed: list[dict]) -> None:
        """The real pooling job, then partners work the routes: one finished, one in progress, the rest waiting."""
        gw = self.gw
        time.sleep(5)  # let quality.graded move the passed orders to READY_FOR_DISPATCH
        due = today().isoformat()
        pools: list[dict] = []

        def run_pooling() -> bool:
            nonlocal pools
            pools = gw.call(
                "POST", f"/logistics/pools/run?pool_date={due}", token=self.admin
            ).json()["pools"]
            return bool(pools)

        if not _wait(run_pooling, 90, interval=3):
            self.s.notes.append("no orders were ready for dispatch, so no pools were made")
            return
        self.s.pools = len(pools)
        delivered: list[str] = []
        ranked = sorted(pools, key=lambda p: -len(p["stops"]))
        for i, pool in enumerate(ranked[:2]):
            phone = (
                self.partner_phone.get(pool.get("partnerId") or "")
                or D.PARTNERS[i % len(D.PARTNERS)][1]
            )
            token = self.token(phone)
            stops = sorted(pool["stops"], key=lambda s: s["seq"])
            for stop in (s for s in stops if s["stopType"] == "PICKUP"):
                gw.call("POST", f"/logistics/stops/{stop['id']}/pickup", token=token, json={})
            drops = [s for s in stops if s["stopType"] == "DROP"]
            for stop in drops[: (len(drops) if i == 0 else 1)]:
                media = self.staff_photo(
                    token, "delivery", "DELIVERY", stop["id"], stop["lat"], stop["lng"], 0
                )
                gw.call(
                    "POST",
                    f"/logistics/stops/{stop['id']}/deliver",
                    token=token,
                    json={
                        "photoMediaId": media,
                        "otp": stop.get("otp") or "0000",
                        "notes": "Handed over at the door.",
                    },
                )
                delivered.append(stop["orderId"])
        self.s.delivered_orders = len(delivered)
        by_id = {o["id"]: o for o in placed}
        reviews = iter(REVIEWS)
        for order_id in delivered:
            o = by_id.get(order_id)
            review = next(reviews, None)
            if o is None or review is None:
                continue
            token = self.token(D.CUSTOMERS[o["customer"]].phone)

            def completed(order_id: str = order_id, token: str = token) -> bool:
                return (
                    gw.call("GET", f"/order/orders/{order_id}", token=token).json()["status"]
                    == "COMPLETED"
                )

            if _wait(completed, 30):
                score, comment = review
                gw.call(
                    "POST",
                    f"/order/orders/{order_id}/rating",
                    token=token,
                    json={"score": score, "comment": comment},
                )
                self.s.ratings += 1


def run_demo(gw: Gateway, admin_phone: str, admin_password: str) -> Summary:
    demo = Demo(gw, admin_phone, admin_password)
    first = D.FARMERS[0].phone
    if (
        gw.http.post("/identity/login", json={"phone": first, "password": PASSWORD}).status_code
        == 200
    ):
        raise SeedError(
            "The demo data is already there. Reset the databases first (see docs/DEMO.md)."
        )
    demo.people()
    demo.catalog()
    demo.land()
    demo.finish_harvests(demo.reservations())
    placed = demo.orders()
    demo.lifecycle(placed)
    demo.deliveries(placed)
    return demo.s
