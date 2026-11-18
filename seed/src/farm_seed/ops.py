"""Operations seeding: takes the placed orders through packing, inspection, pooling and delivery so
every part of the apps has data. Everything goes through the public API (the real pooling job
included); the steps that depend on events poll until the event has been handled.

Orders are split into fixed groups by their position, so the result is the same on every run:

    k % 12 == 0      left CONFIRMED
    k % 12 == 1      cancelled by the farmer
    k % 12 in 2, 3   PACKED, inspection still waiting (shows in the inspector queue)
    k % 12 == 4      PACKED, inspection FAILED, re-harvest asked
    k % 12 == 5      PACKED, inspection FAILED, lower grade accepted (goes on to dispatch)
    k % 12 >= 6      PACKED, inspection PASSED -> READY_FOR_DISPATCH -> pooled

Pools are then handled in turn: delivered, in progress (pickups done) or left waiting.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import uuid4

from farm_seed.client import Gateway, SeedError
from farm_seed.data import PASSWORD
from farm_seed.v1 import _ensure_user, _point

INSPECTORS = [
    ("Inspector Asha", "+919000300001", 21.2514, 81.6296),  # Raipur
    ("Inspector Bharat", "+919000300002", 19.9975, 73.7898),  # Nashik
    ("Inspector Charu", "+919000300003", 26.9124, 75.7873),  # Jaipur
]
PARTNERS = [
    ("Rider Deepak", "+919000400001"),
    ("Rider Esha", "+919000400002"),
    ("Rider Farhan", "+919000400003"),
    ("Rider Gauri", "+919000400004"),
]
MAX_LIFECYCLE_ORDERS = 90  # the inspection list returns at most 100 rows


@dataclass
class OpsSummary:
    inspectors: int = 0
    partners: int = 0
    cancelled: int = 0
    packed: int = 0
    inspections_reported: int = 0
    inspections_waiting: int = 0
    pools: int = 0
    pools_delivered: int = 0
    pools_in_progress: int = 0
    notes: list[str] = field(default_factory=list)


def _wait(check: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _login(gw: Gateway, cache: dict[str, str], phone: str) -> str:
    if phone not in cache:
        cache[phone] = gw.login(phone, PASSWORD)["accessToken"]
    return cache[phone]


def _staff(gw: Gateway, admin: str, summary: OpsSummary, tokens: dict[str, str]) -> dict[str, str]:
    """Create inspectors (role granted by the admin) and delivery partners. Returns inspector
    token by user id."""
    by_id: dict[str, str] = {}
    for name, phone, lat, lng in INSPECTORS:
        user = _ensure_user(gw, name, phone, ["customer"], lat, lng)["user"]
        if "inspector" not in user.get("roles", []):
            gw.call(
                "PUT",
                f"/identity/users/{user['id']}/roles",
                token=admin,
                json={"roles": ["inspector"]},
            )
        tokens.pop(phone, None)  # roles changed: old tokens are revoked
        token = _login(gw, tokens, phone)
        gw.call(
            "PUT",
            "/quality/inspectors/me/location",
            token=token,
            json={"location": _point(lat, lng), "available": True},
        )
        by_id[user["id"]] = token
        summary.inspectors += 1
    for name, phone in PARTNERS:
        _ensure_user(gw, name, phone, ["delivery"])
        summary.partners += 1
    return by_id


def _confirmed_orders(
    gw: Gateway, farmer_phones: list[str], tokens: dict[str, str]
) -> list[tuple[str, int]]:
    """(order id, farmer index) of CONFIRMED orders, oldest first, at most MAX_LIFECYCLE_ORDERS."""
    found: list[tuple[str, str, int]] = []
    for f_idx, phone in enumerate(farmer_phones):
        page = gw.call(
            "GET",
            "/order/orders?role=farmer&status=CONFIRMED&limit=100",
            token=_login(gw, tokens, phone),
        ).json()
        rows = page["items"] if isinstance(page, dict) else page
        found += [
            (row["createdAt"], row["id"], f_idx) for row in rows if row["status"] == "CONFIRMED"
        ]
    found.sort()
    return [(order_id, f_idx) for _, order_id, f_idx in found[:MAX_LIFECYCLE_ORDERS]]


def seed_operations(
    gw: Gateway,
    farmer_phones: list[str],
    admin_phone: str,
    admin_password: str,
    wait_seconds: float = 90,
) -> OpsSummary:
    """Orders are found through each farmer's order list, so this also works on a later run."""
    summary = OpsSummary()
    tokens: dict[str, str] = {}
    admin = gw.login(admin_phone, admin_password)["accessToken"]
    inspector_tokens = _staff(gw, admin, summary, tokens)
    orders = _confirmed_orders(gw, farmer_phones, tokens)

    # --- cancel and pack ---
    packed: list[str] = []
    group: dict[str, int] = {}
    for k, (order_id, f_idx) in enumerate(orders):
        group[order_id] = k % 12
        token = _login(gw, tokens, farmer_phones[f_idx])
        if k % 12 == 0:
            continue
        if k % 12 == 1:
            done = gw.call(
                "POST",
                f"/order/orders/{order_id}/cancel",
                token=token,
                json={"reason": "Seeded cancellation"},
                ok=(200, 403, 409),
            )
            summary.cancelled += 1 if done.status_code == 200 else 0
            continue
        done = gw.call("POST", f"/order/orders/{order_id}/pack", token=token, ok=(200, 403, 409))
        if done.status_code == 200:
            packed.append(order_id)
            summary.packed += 1

    # --- inspections: created by the order.packed event ---
    def inspections() -> dict[str, dict]:
        rows = gw.call("GET", "/quality/inspections", token=admin).json()
        return {r["subjectId"]: r for r in rows if r["subjectType"] == "ORDER"}

    wanted = {o for o in packed}
    if not _wait(lambda: wanted <= inspections().keys(), wait_seconds):
        summary.notes.append("not every packed order got an inspection in time (is RabbitMQ up?)")
    found = inspections()
    for order_id in packed:
        inspection = found.get(order_id)
        g = group[order_id]
        if inspection is None or inspection["status"] != "PENDING" or g in (2, 3):
            summary.inspections_waiting += 1 if inspection and g in (2, 3) else 0
            continue
        mine = inspector_tokens.get(inspection.get("inspectorId") or "", admin)
        report: dict = {
            "grade": "A" if g % 2 else "B",
            "moisturePct": 11.5,
            "pestFound": False,
            "weightKg": 20,
            "result": "PASS",
            "note": "Seeded inspection",
        }
        if g == 4:
            report.update(result="FAIL", grade="C", pestFound=True, resolution="REHARVEST")
        elif g == 5:
            report.update(
                result="FAIL", grade="B", resolution="ACCEPT_LOWER_GRADE", acceptedGrade="C"
            )
        gw.call("POST", f"/quality/inspections/{inspection['id']}/report", token=mine, json=report)
        summary.inspections_reported += 1

    # --- pooling: the real job over every READY_FOR_DISPATCH order ---
    # wait for quality.graded to move the passed orders on before running the job
    time.sleep(min(wait_seconds, 5))
    due = (date.today() + timedelta(days=14)).isoformat()
    pools: list[dict] = []

    def run_pooling() -> bool:
        nonlocal pools
        response = gw.call("POST", f"/logistics/pools/run?pool_date={due}", token=admin).json()
        pools = response["pools"]
        return bool(pools)

    if not _wait(run_pooling, wait_seconds, interval=3):
        summary.notes.append("no orders were ready for dispatch, so no pools were made")
    summary.pools = len(pools)

    # --- delivery: partners work through the pools ---
    for i, pool in enumerate(pools):
        partner_phone = PARTNERS[i % len(PARTNERS)][1]
        token = _login(gw, tokens, partner_phone)
        mode = i % 4  # 0 and 3: delivered, 1: pickups only (in progress), 2: left waiting
        if mode == 2:
            continue
        stops = sorted(pool["stops"], key=lambda s: s["seq"])
        for stop in (s for s in stops if s["stopType"] == "PICKUP"):
            gw.call("POST", f"/logistics/stops/{stop['id']}/pickup", token=token, json={})
        if mode == 1:
            summary.pools_in_progress += 1
            continue
        for stop in (s for s in stops if s["stopType"] == "DROP"):
            gw.call(
                "POST",
                f"/logistics/stops/{stop['id']}/deliver",
                token=token,
                json={"photoMediaId": str(uuid4()), "otp": stop.get("otp") or "0000"},
            )
        summary.pools_delivered += 1
    return summary


__all__ = ["OpsSummary", "SeedError", "seed_operations"]
