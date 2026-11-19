import json
import re
from uuid import uuid4

import httpx

from farm_seed import ops
from farm_seed.client import Gateway
from farm_seed.data import PASSWORD

FARMERS = ["+919000100000", "+919000100001"]


class FakeOps:
    """Stands in for identity, order, quality and logistics: enough to walk the lifecycle."""

    def __init__(self, orders: int):
        self.users: dict[str, dict] = {}
        self.orders = {}
        self.inspections: dict[str, dict] = {}
        self.pools: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        for n in range(orders):
            self.orders[str(uuid4())] = {"status": "CONFIRMED", "farmer": n % 2, "n": n}

    def user(self, phone: str) -> dict:
        return self.users.setdefault(phone, {"id": str(uuid4()), "roles": ["customer"]})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        query = request.url.query.decode()
        body = json.loads(request.content) if request.content else {}
        who = request.headers.get("Authorization", "").removeprefix("Bearer tok:")
        self.calls.append((request.method, path))
        if path == "/identity/login":
            user = self.user(body["phone"])
            assert body["password"] == PASSWORD or body["phone"] == "+910000000000"
            phone = body["phone"]
            return httpx.Response(
                200,
                json={"accessToken": f"tok:{phone}", "user": {**user, "phone": phone}},
            )
        if path == "/identity/register":
            if body["phone"] in self.users:
                return httpx.Response(409, json={})
            self.users[body["phone"]] = {"id": str(uuid4()), "roles": body["roles"]}
            user = {**self.users[body["phone"]], "phone": body["phone"]}
            return httpx.Response(201, json=user)  # UserOut: register returns no tokens
        if m := re.fullmatch(r"/identity/users/(.+)/roles", path):
            next(u for u in self.users.values() if u["id"] == m[1])["roles"] = body["roles"]
            return httpx.Response(200, json={})
        if path == "/quality/inspectors/me/location":
            return httpx.Response(200, json={})
        if path == "/order/orders" and method_is_get(request):
            farmer = FARMERS.index(who)
            rows = [
                {
                    "id": oid,
                    "status": o["status"],
                    "createdAt": f"2026-11-1{o['n'] % 10}T00:00:{o['n']:02d}",
                }
                for oid, o in self.orders.items()
                if o["farmer"] == farmer
                and "status=CONFIRMED" in query
                and o["status"] == "CONFIRMED"
            ]
            return httpx.Response(200, json={"items": rows})
        if m := re.fullmatch(r"/order/orders/(.+)/(pack|cancel)", path):
            order = self.orders[m[1]]
            order["status"] = "PACKED" if m[2] == "pack" else "CANCELLED"
            if m[2] == "pack":  # the order.packed event creates an inspection
                self.inspections[m[1]] = {
                    "id": str(uuid4()),
                    "subjectType": "ORDER",
                    "subjectId": m[1],
                    "status": "PENDING",
                    "inspectorId": None,
                }
            return httpx.Response(200, json={})
        if path == "/quality/inspections":
            return httpx.Response(200, json=list(self.inspections.values()))
        if m := re.fullmatch(r"/quality/inspections/(.+)/report", path):
            row = next(i for i in self.inspections.values() if i["id"] == m[1])
            row["status"] = "REPORTED"
            if body["result"] == "PASS" or body.get("resolution") == "ACCEPT_LOWER_GRADE":
                self.orders[row["subjectId"]]["status"] = "READY_FOR_DISPATCH"
            return httpx.Response(200, json=row)
        if path == "/logistics/pools/run":
            ready = [o for o, v in self.orders.items() if v["status"] == "READY_FOR_DISPATCH"]
            self.pools = [
                {
                    "id": str(uuid4()),
                    "stops": [
                        {"id": str(uuid4()), "seq": 1 + 2 * j + s, "stopType": t, "otp": None}
                        for j, _ in enumerate(ready[i : i + 2])
                        for s, t in enumerate(["PICKUP", "DROP"])
                    ],
                }
                for i in range(0, len(ready), 2)
            ]
            return httpx.Response(202, json={"poolsCreated": len(self.pools), "pools": self.pools})
        if re.fullmatch(r"/logistics/stops/.+/(pickup|deliver)", path):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {path}")


def method_is_get(request: httpx.Request) -> bool:
    return request.method == "GET"


def test_operations_cover_each_outcome_and_delivery(monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda _s: None)
    fake = FakeOps(orders=24)
    for phone in FARMERS:
        fake.user(phone)
    gw = Gateway("http://gw/api/v1", transport=httpx.MockTransport(fake.handle))

    summary = ops.seed_operations(gw, FARMERS, "+910000000000", "pw", wait_seconds=1)

    states = [o["status"] for o in fake.orders.values()]
    assert states.count("CANCELLED") == summary.cancelled > 0
    assert summary.packed > 0 and summary.inspections_reported > 0
    assert summary.inspections_waiting > 0  # the PACKED-but-not-inspected group
    assert summary.pools == len(fake.pools) > 0
    assert summary.pools_delivered > 0 and summary.pools_in_progress > 0
    assert summary.inspectors == len(ops.INSPECTORS) and summary.partners == len(ops.PARTNERS)
    assert any(path.endswith("/deliver") for _, path in fake.calls)
    assert all(
        u["roles"] == ["inspector"] for p, u in fake.users.items() if p.startswith("+9190003")
    )
