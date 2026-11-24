"""API checks for the features the V1/V2 journeys do not walk through: accounts and sessions, admin
access, editing and deleting, rejecting and cancelling, notifications, devices, media lifecycle,
order cancellation with stock coming back, and the gateway blocking internal routes.

    uv run --package farm-seed python loadtest/api_checks.py --base-url http://<gateway>/api/v1 \\
        --admin-phone ... --admin-password ...

People made here have +917000 9xxxxx phones (names start with "Check"), so loadtest/cleanup.py
--phone-prefix +917000 removes them. Exits 1 if any check fails.
"""

import argparse
import random
import time
from datetime import UTC, datetime, timedelta

import httpx

from farm_seed import photos
from farm_seed.client import Gateway

FAILED: list[str] = []


def check(name: str, cond: object, detail: object = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  ({detail})"), flush=True)
    if not cond:
        FAILED.append(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost/api/v1")
    ap.add_argument("--admin-phone", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--near", default="22.08,82.15", help="lat,lng for the test farm and plot")
    args = ap.parse_args()
    lat0, lng0 = (float(x) for x in args.near.split(","))
    gw = Gateway(args.base_url)
    http = gw.http
    run = random.randint(10000, 99999)
    phone = lambda n: f"+9170009{run % 1000:03d}{n:02d}"  # noqa: E731
    pw = "Check@2026x"

    def call(method: str, path: str, token: str | None = None, body: object = None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        headers.update(kw.pop("headers", {}))
        return http.request(method, path, json=body, headers=headers, **kw)

    def person(n: int, roles: list[str], name: str) -> dict:
        r = call(
            "POST",
            "/identity/register",
            body={
                "name": name,
                "phone": phone(n),
                "password": pw,
                "roles": roles,
                "homeLat": lat0,
                "homeLng": lng0,
            },
        )
        assert r.status_code == 201, r.text
        t = call("POST", "/identity/login", body={"phone": phone(n), "password": pw}).json()
        return {
            "id": t["user"]["id"],
            "token": t["accessToken"],
            "refresh": t["refreshToken"],
            "phone": phone(n),
        }

    admin = gw.login(args.admin_phone, args.admin_password)["accessToken"]

    print("Accounts and sessions")
    cust = person(1, ["customer"], "Check Customer")
    other = person(2, ["customer"], "Check Other")
    r = call(
        "POST",
        "/identity/register",
        body={"name": "Dup", "phone": phone(1), "password": pw, "roles": ["customer"]},
    )
    check("registering a taken phone is refused (409)", r.status_code == 409, r.status_code)
    r = call("POST", "/identity/login", body={"phone": phone(1), "password": "wrong-password"})
    check("wrong password is refused (401)", r.status_code == 401, r.status_code)
    r = call("GET", "/identity/me")
    check("no token: 401", r.status_code == 401, r.status_code)
    r = call("GET", "/identity/me", token="not-a-token")
    check("bad token: 401", r.status_code == 401, r.status_code)
    r = call("PATCH", "/identity/me", cust["token"], {"name": "Check Customer Renamed"})
    check(
        "profile update",
        r.status_code == 200 and r.json()["name"] == "Check Customer Renamed",
        r.text[:200],
    )
    r = call("POST", "/identity/refresh", body={"refreshToken": cust["refresh"]})
    check("refresh gives new tokens", r.status_code == 200, r.text[:200])
    new = r.json()
    r2 = call("POST", "/identity/refresh", body={"refreshToken": cust["refresh"]})
    check("an old refresh token cannot be reused", r2.status_code == 401, r2.status_code)
    cust["token"], cust["refresh"] = new["accessToken"], new["refreshToken"]
    r = call("POST", "/identity/logout", cust["token"], {"refreshToken": cust["refresh"]})
    check("logout", r.status_code == 204, r.status_code)
    r = call("POST", "/identity/refresh", body={"refreshToken": cust["refresh"]})
    check("refresh after logout is refused", r.status_code == 401, r.status_code)
    r = call("POST", "/identity/otp/request", body={"phone": phone(1)})
    code = (r.json() or {}).get("devCode") if r.status_code == 202 else None
    check("OTP requested (dev mode returns the code)", r.status_code == 202 and code, r.text[:200])
    r = call("POST", "/identity/otp/request", body={"phone": phone(1)})
    check("a second OTP straight away is refused (429)", r.status_code == 429, r.status_code)
    r = call(
        "POST",
        "/identity/otp/verify",
        body={"phone": phone(1), "code": "000000" if code != "000000" else "111111"},
    )
    check("wrong OTP refused", r.status_code in (400, 401, 422), r.status_code)
    r = call("POST", "/identity/otp/verify", body={"phone": phone(1), "code": code})
    check("OTP sign-in", r.status_code == 200 and r.json().get("accessToken"), r.text[:200])
    cust["token"] = r.json()["accessToken"]
    r = call("POST", "/identity/me/roles", cust["token"], {"role": "farmer"})
    check(
        "customer adds the farmer role",
        r.status_code == 200 and "farmer" in r.json().get("roles", []),
        r.text[:200],
    )

    print("Admin access")
    r = call("GET", "/identity/users?limit=5", cust["token"])
    check("a customer cannot list users (403)", r.status_code == 403, r.status_code)
    r = call("GET", "/identity/users?limit=5", admin)
    check("admin lists users", r.status_code == 200 and r.json()["items"], r.status_code)
    r = call("GET", f"/identity/users/{cust['id']}", admin)
    check("admin reads one user", r.status_code == 200, r.status_code)
    r = call("POST", "/logistics/pools/run", cust["token"])
    check("a customer cannot run pooling (403)", r.status_code == 403, r.status_code)
    r = call("GET", "/logistics/pools/batch", admin)
    check("admin previews the shared-delivery batch", r.status_code == 200, r.text[:200])
    for path in (
        "/identity/internal/users/" + cust["id"],
        "/catalog/internal/geo/farm/x",
        "/order/internal/orders/ready-for-dispatch",
    ):
        r = call("GET", path, admin)
        check(
            f"gateway blocks {path.split('/internal')[0]}/internal/...",
            r.status_code in (403, 404),
            r.status_code,
        )

    print("Catalog: farms and listings")
    farmer = person(3, ["farmer"], "Check Farmer")
    r = call(
        "POST",
        "/catalog/farms",
        farmer["token"],
        {
            "name": "Not yet",
            "district": "Bilaspur",
            "location": {"type": "Point", "coordinates": [lng0, lat0]},
        },
    )
    check("an unverified farmer cannot open a farm", r.status_code == 403, r.status_code)
    call("POST", f"/identity/users/{farmer['id']}/verify-farmer", admin)
    farmer["token"] = gw.login(farmer["phone"], pw)["accessToken"]
    farm = call(
        "POST",
        "/catalog/farms",
        farmer["token"],
        {
            "name": f"Check Farm {run}",
            "district": "Bilaspur",
            "state": "Chhattisgarh",
            "location": {"type": "Point", "coordinates": [lng0, lat0]},
        },
    ).json()
    r = call("PATCH", f"/catalog/farms/{farm['id']}", farmer["token"], {"village": "Checkpur"})
    check(
        "farm update", r.status_code == 200 and r.json().get("village") == "Checkpur", r.text[:200]
    )
    r = call("PATCH", f"/catalog/farms/{farm['id']}", other["token"], {"village": "Hacked"})
    check("someone else cannot edit the farm", r.status_code in (403, 404), r.status_code)
    today = (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date().isoformat()
    listing = call(
        "POST",
        "/catalog/listings",
        farmer["token"],
        {
            "farmId": farm["id"],
            "cropId": "onion",
            "qtyAvailable": 50,
            "unit": "kg",
            "pricePerUnit": 30,
            "harvestDate": today,
            "isOrganic": False,
        },
    ).json()
    r = call(
        "POST",
        "/catalog/listings",
        farmer["token"],
        {
            "farmId": farm["id"],
            "cropId": "onion",
            "qtyAvailable": -5,
            "unit": "kg",
            "pricePerUnit": 30,
            "harvestDate": today,
        },
    )
    check("negative stock is rejected (422)", r.status_code == 422, r.status_code)
    r = call("GET", f"/catalog/listings/search?lat={lat0}&lng={lng0}&radius_km=5&crop=onion")
    check(
        "search finds the new listing",
        any(f["id"] == listing["id"] for f in r.json()["features"]),
        r.status_code,
    )
    r = call("PATCH", f"/catalog/listings/{listing['id']}", farmer["token"], {"pricePerUnit": 35})
    check("price change", r.status_code == 200 and r.json()["pricePerUnit"] == 35, r.text[:200])

    print("Orders: cancel brings the stock back")
    intent = call(
        "POST", "/order/orders/payments/intent", other["token"], {"amount": 1, "kind": "FULL"}
    ).json()
    addr = {
        "line1": "1 Check Road",
        "city": "Bilaspur",
        "pincode": "495001",
        "location": {"type": "Point", "coordinates": [lng0 + 0.01, lat0]},
    }
    body = {
        "items": [{"listingId": listing["id"], "qty": 10}],
        "deliveryAddress": addr,
        "deliveryDate": today,
        "paymentIntentId": intent["id"],
    }
    key = {"Idempotency-Key": f"check-{run}"}
    r = call("POST", "/order/orders", other["token"], body, headers=key)
    order = r.json()["orders"][0]
    r2 = call("POST", "/order/orders", other["token"], body, headers=key)
    check(
        "the same Idempotency-Key returns the same order",
        r2.status_code in (200, 201) and r2.json()["orders"][0]["id"] == order["id"],
        r2.text[:200],
    )
    left = call("GET", f"/catalog/listings/{listing['id']}").json()["qtyAvailable"]
    check("stock reserved (50 -> 40)", left == 40, left)
    r = call(
        "POST",
        "/order/orders",
        other["token"],
        {
            **body,
            "items": [{"listingId": listing["id"], "qty": 999}],
            "paymentIntentId": call(
                "POST",
                "/order/orders/payments/intent",
                other["token"],
                {"amount": 1, "kind": "FULL"},
            ).json()["id"],
        },
    )
    check("ordering more than the stock is refused", r.status_code in (409, 422), r.status_code)
    r = call("GET", f"/logistics/orders/{order['id']}/track", cust["token"])
    t = r.json() if r.status_code == 200 else {}
    leaked = any(t.get(k) for k in ("stop", "partner", "partnerName", "partnerPhone", "partnerLat"))
    check(
        "another customer learns nothing from tracking",
        r.status_code in (403, 404) or not leaked,
        t,
    )
    r = call("POST", f"/order/orders/{order['id']}/cancel", cust["token"], {"reason": "not mine"})
    check("another customer cannot cancel it", r.status_code in (403, 404), r.status_code)
    r = call("POST", f"/order/orders/{order['id']}/cancel", other["token"], {"reason": "Check"})
    check(
        "customer cancels", r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text[:200]
    )
    back = None
    for _ in range(20):
        back = call("GET", f"/catalog/listings/{listing['id']}").json()["qtyAvailable"]
        if back == 50:
            break
        time.sleep(1)
    check("stock released after the cancel (back to 50)", back == 50, back)
    r = call("POST", f"/order/orders/{order['id']}/cancel", other["token"], {"reason": "again"})
    check("cancelling twice is refused (409)", r.status_code == 409, r.status_code)
    r = call("POST", f"/order/orders/{order['id']}/rating", other["token"], {"score": 5})
    check("a cancelled order cannot be rated", r.status_code in (409, 422), r.status_code)

    print("Land: reject and cancel")
    s = 0.0006
    py = lat0 + 0.03 + (run % 97) * 0.002  # each run its own spot: plots may not overlap
    ring = [[lng0, py], [lng0 + s, py], [lng0 + s, py + s], [lng0, py + s], [lng0, py]]
    plot = call(
        "POST",
        "/land/plots",
        farmer["token"],
        {"title": f"Check plot {run}", "boundary": {"type": "Polygon", "coordinates": [ring]}},
    ).json()
    r = call("PATCH", f"/land/plots/{plot['id']}", farmer["token"], {"waterSource": "Canal"})
    check("plot update", r.status_code == 200, r.text[:200])
    start = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    end = (datetime.now(UTC) + timedelta(days=100)).date().isoformat()
    season = call(
        "POST",
        f"/land/plots/{plot['id']}/seasons",
        farmer["token"],
        {
            "season": "Rabi",
            "startDate": start,
            "endDate": end,
            "price": 20,
            "allowedCrops": ["wheat"],
        },
    ).json()
    r = call(
        "POST",
        f"/land/plots/{plot['id']}/seasons",
        farmer["token"],
        {
            "season": "Rabi",
            "startDate": start,
            "endDate": end,
            "price": 20,
            "allowedCrops": ["wheat"],
        },
    )
    check("an overlapping season is refused", r.status_code in (409, 422), r.status_code)
    r = call(
        "POST",
        "/land/reservations",
        other["token"],
        {"plotSeasonId": season["id"], "cropId": "rice", "expectedQtyKg": 100},
    )
    check("a crop the season does not allow is refused", r.status_code in (409, 422), r.status_code)
    res1 = call(
        "POST",
        "/land/reservations",
        other["token"],
        {"plotSeasonId": season["id"], "cropId": "wheat", "expectedQtyKg": 100},
    ).json()
    r = call("POST", f"/land/reservations/{res1['id']}/accept", other["token"])
    check(
        "the customer cannot accept their own request", r.status_code in (403, 404), r.status_code
    )
    r = call("POST", f"/land/reservations/{res1['id']}/reject", farmer["token"], {})
    check(
        "farmer rejects a request",
        r.status_code == 200 and r.json()["status"] == "REJECTED",
        r.text[:200],
    )
    res2 = call(
        "POST",
        "/land/reservations",
        other["token"],
        {"plotSeasonId": season["id"], "cropId": "wheat", "expectedQtyKg": 100},
    ).json()
    r = call("POST", f"/land/reservations/{res2['id']}/cancel", other["token"], {})
    check(
        "customer cancels a request",
        r.status_code == 200 and r.json()["status"] == "CANCELLED",
        r.text[:200],
    )

    print("Notifications and devices")
    time.sleep(3)
    rows = call("GET", "/notification/notifications?limit=50", farmer["token"]).json()["items"]
    titles = {n["title"] for n in rows}
    check("farmer was told: verified, new order, land request", len(rows) >= 3, sorted(titles))
    unread = call("GET", "/notification/notifications/unread-count", farmer["token"]).json()
    first = rows[0]["id"]
    call("POST", f"/notification/notifications/{first}/read", other["token"])
    still = call("GET", "/notification/notifications/unread-count", farmer["token"]).json()
    check(
        "someone else cannot mark the farmer's notification read", still == unread, (unread, still)
    )
    r = call("POST", f"/notification/notifications/{first}/read", farmer["token"])
    check("mark one read", r.status_code in (200, 204), r.status_code)
    call("POST", "/notification/notifications/read-all", farmer["token"])
    after = call("GET", "/notification/notifications/unread-count", farmer["token"]).json()
    check(
        "read-all clears the unread count",
        list(after.values())[0] == 0 and list(unread.values())[0] > 0,
        (unread, after),
    )
    tok = f"check-device-{run}"
    r = call(
        "POST",
        "/notification/notifications/devices",
        other["token"],
        {"token": tok, "platform": "android"},
    )
    check("register a device for push", r.status_code in (200, 201, 204), r.text[:200])
    r = call("DELETE", f"/notification/notifications/devices/{tok}", other["token"])
    check("unregister the device", r.status_code in (200, 204), r.status_code)

    print("Media: upload, process, list, delete")
    media = photos.upload(
        gw, farmer["token"], "stage_sown", "LISTING", listing["id"], lat0, lng0, 0
    )
    m = call("GET", f"/media/{media}", farmer["token"]).json()
    check(
        "photo processed: READY with a thumbnail",
        m.get("status") == "READY" and m.get("thumbUrl"),
        {k: m.get(k) for k in ("status", "geoStatus")},
    )
    thumb = httpx.get(m["thumbUrl"], timeout=10) if m.get("thumbUrl") else None
    check(
        "thumbnail is downloadable",
        thumb is not None and thumb.status_code == 200,
        thumb.status_code if thumb else None,
    )
    r = call("GET", f"/media/owners/LISTING/{listing['id']}")
    items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    check("listed under its listing", any(x["id"] == media for x in items), r.text[:200])
    r = call("DELETE", f"/media/{media}", other["token"])
    check("someone else cannot delete the photo", r.status_code in (403, 404), r.status_code)
    r = call("DELETE", f"/media/{media}", farmer["token"])
    check("owner deletes the photo", r.status_code in (200, 204), r.status_code)
    r = call("DELETE", f"/catalog/listings/{listing['id']}", farmer["token"])
    check("delete the listing", r.status_code in (200, 204), r.status_code)
    r = call("GET", f"/catalog/listings/{listing['id']}")
    gone = r.status_code == 404 or (r.status_code == 200 and r.json().get("status") != "ACTIVE")
    check("a deleted listing is gone from the shop", gone, r.status_code)

    print(
        f"\n{'ALL CHECKS PASSED' if not FAILED else f'{len(FAILED)} FAILED: ' + '; '.join(FAILED)}"
    )
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
