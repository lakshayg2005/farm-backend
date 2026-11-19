import json
import re
from uuid import uuid4

import httpx

from catalog_service.crops import CROPS as CATALOG_CROPS
from farm_seed.client import Gateway
from farm_seed.data import CROPS, build_dataset, build_v2_plan
from farm_seed.v1 import seed_v1
from farm_seed.v2 import seed_v2


def test_dataset_is_deterministic():
    assert build_dataset(5, 5) == build_dataset(5, 5)
    assert build_dataset(5, 5, seed=1) != build_dataset(5, 5)


def test_every_seeded_crop_exists_in_the_catalog_and_units_match():
    catalog = {c[0]: c[3] for c in CATALOG_CROPS}
    for crop_id, (unit, _, _) in CROPS.items():
        assert catalog[crop_id] == unit


class FakeGateway:
    """Just enough of identity + catalog to run the seeder, with duplicate-phone and idempotency rules."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.farms: dict[str, list[dict]] = {}
        self.listings: list[dict] = []
        self.idem: dict[str, httpx.Response] = {}
        self.admin_id = str(uuid4())
        self.plots: dict[str, list[dict]] = {}
        self.reservations: list[dict] = []
        self.intents: list[dict] = []
        self.orders: list[dict] = []

    @property
    def listings_by_farmer(self):
        # the fake catalog does not track owners; give every farmer two active listings
        return {
            f"{u['id']}": [{"id": str(uuid4()), "status": "ACTIVE"} for _ in range(2)]
            for u in self.users.values()
        }

    def tokens(self, user):
        return {
            "accessToken": f"tok:{user['id']}",
            "refreshToken": "r",
            "expiresIn": 900,
            "user": user,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else {}
        who = request.headers.get("Authorization", "").removeprefix("Bearer tok:")
        key = request.headers.get("Idempotency-Key")
        if key and key in self.idem:
            return self.idem[key]
        out = self._route(request.method, path, body, who)
        if key:
            self.idem[key] = out
        return out

    def _route(self, method, path, body, who):
        if path == "/identity/login":
            if body["phone"] == "+910000000000":
                return httpx.Response(
                    200, json=self.tokens({"id": self.admin_id, "farmerVerified": False})
                )
            return httpx.Response(200, json=self.tokens(self.users[body["phone"]]))
        if path == "/identity/register":
            if body["phone"] in self.users:
                return httpx.Response(409, json={"error": {"code": "PHONE_TAKEN"}})
            user = {"id": str(uuid4()), "farmerVerified": False, "roles": body["roles"]}
            self.users[body["phone"]] = user
            return httpx.Response(201, json=user)  # UserOut: register returns no tokens
        if m := re.fullmatch(r"/identity/users/(.+)/verify-farmer", path):
            assert who == self.admin_id
            next(u for u in self.users.values() if u["id"] == m[1])["farmerVerified"] = True
            return httpx.Response(200, json={})
        if path == "/catalog/crops":
            return httpx.Response(200, json=[{"id": c} for c in CROPS])
        if path == "/catalog/farms/mine":
            return httpx.Response(200, json={"items": self.farms.get(who, [])})
        if path == "/catalog/farms":
            farm = {"id": str(uuid4()), "name": body["name"]}
            self.farms.setdefault(who, []).append(farm)
            return httpx.Response(201, json=farm)
        if path == "/catalog/listings":
            self.listings.append(body)
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/catalog/listings/mine":
            mine = [x for x in self.listings_by_farmer.get(who, [])]
            return httpx.Response(200, json={"items": mine})
        if path == "/land/plots/mine":
            return httpx.Response(200, json=self.plots.get(who, []))
        if path == "/land/plots":
            plot = {"id": str(uuid4())}
            self.plots.setdefault(who, []).append(plot)
            return httpx.Response(201, json=plot)
        if re.fullmatch(r"/land/plots/.+/seasons", path):
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/land/reservations":
            self.reservations.append(body)
            return httpx.Response(201, json={"id": str(uuid4())})
        if re.fullmatch(r"/land/reservations/.+/accept", path):
            return httpx.Response(200, json={"deposit": 800})
        if re.fullmatch(r"/land/reservations/.+/updates", path):
            return httpx.Response(201, json={})
        if path == "/order/orders/payments/intent":
            self.intents.append(body)
            return httpx.Response(201, json={"id": str(uuid4()), "status": "SUCCEEDED"})
        if path == "/order/orders":
            self.orders.append(body)
            return httpx.Response(201, json={"orders": []})
        return httpx.Response(404, json={})


def test_seeding_twice_creates_everything_once():
    fake = FakeGateway()
    gw = Gateway("http://gw/api/v1", transport=httpx.MockTransport(fake.handle))
    data = build_dataset(4, 6)

    first = seed_v1(gw, data, "+910000000000", "pw")
    listings_after_first = len(fake.listings)
    second = seed_v1(gw, data, "+910000000000", "pw")

    assert first.farmers == 4 and first.customers == 6
    assert first.listings == listings_after_first > 0
    assert second.listings == 0  # farms already existed: nothing was created again
    assert len(fake.listings) == listings_after_first
    assert sum(len(v) for v in fake.farms.values()) == first.farms
    assert all(u["farmerVerified"] for p, u in fake.users.items() if "farmer" in u["roles"])


def test_v2_seeding_creates_plots_reservations_and_orders_once():
    fake = FakeGateway()
    gw = Gateway("http://gw/api/v1", transport=httpx.MockTransport(fake.handle))
    data = build_dataset(6, 8)
    plan = build_v2_plan(data, orders=15)
    seed_v1(gw, data, "+910000000000", "pw")

    first = seed_v2(gw, data, plan)
    second = seed_v2(gw, data, plan)

    assert first.plots == sum(len(v) for v in plan.plots.values())
    assert first.seasons == first.plots * 2
    assert first.reservations == len(fake.reservations) > 0
    assert first.orders == 15 == len(fake.orders)
    assert second.plots == 0 and second.reservations == 0  # plots exist: nothing new
    assert any(i["kind"] == "DEPOSIT" for i in fake.intents)


def test_v2_plan_is_deterministic_and_plots_do_not_overlap_within_a_farmer():
    data = build_dataset(6, 8)
    assert build_v2_plan(data, 10) == build_v2_plan(data, 10)
    for specs in build_v2_plan(data, 0).plots.values():
        boxes = [(min(p[0] for p in s.ring), max(p[0] for p in s.ring)) for s in specs]
        for a, b in zip(boxes, boxes[1:], strict=False):
            assert a[1] < b[0]
