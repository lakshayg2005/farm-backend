"""V1 seeding: farmers (verified), farms, produce listings and customers, all through the API."""

from dataclasses import asdict, dataclass

from farm_seed.client import Gateway, SeedError
from farm_seed.data import PASSWORD, Dataset


@dataclass
class Summary:
    farmers: int = 0
    farms: int = 0
    listings: int = 0
    customers: int = 0


def _ensure_user(
    gw: Gateway,
    name: str,
    phone: str,
    roles: list[str],
    lat: float | None = None,
    lng: float | None = None,
) -> dict:
    """Register (unless a previous run already did), then sign in. Returns the token response."""
    body: dict = {"name": name, "phone": phone, "password": PASSWORD, "roles": roles}
    if lat is not None:
        body.update(homeLat=lat, homeLng=lng)
    # Register returns the new user only (no tokens), and 409 means a previous run made it:
    # either way, sign in to get tokens.
    gw.call("POST", "/identity/register", json=body, ok=(201, 409))
    return gw.login(phone, PASSWORD)


def _point(lat: float, lng: float) -> dict:
    return {"type": "Point", "coordinates": [lng, lat]}


def seed_v1(gw: Gateway, data: Dataset, admin_phone: str, admin_password: str) -> Summary:
    admin = gw.login(admin_phone, admin_password)["accessToken"]
    crops = {c["id"] for c in gw.call("GET", "/catalog/crops").json()}
    summary = Summary()

    for f_idx, farmer in enumerate(data.farmers):
        tokens = _ensure_user(gw, farmer.name, farmer.phone, ["farmer"])
        user_id = tokens["user"]["id"]
        if not tokens["user"].get("farmerVerified"):
            gw.call("POST", f"/identity/users/{user_id}/verify-farmer", token=admin)
            tokens = gw.login(farmer.phone, PASSWORD)  # new token carries the verified state
        token = tokens["accessToken"]
        summary.farmers += 1

        existing = {
            f["name"]: f for f in gw.call("GET", "/catalog/farms/mine", token=token).json()["items"]
        }
        for k, farm in enumerate(farmer.farms):
            made = (
                existing.get(farm.name)
                or gw.call(
                    "POST",
                    "/catalog/farms",
                    token=token,
                    idempotency_key=f"seed-farm-{f_idx:04d}-{k}",
                    json={
                        "name": farm.name,
                        "village": farm.village,
                        "district": farm.district,
                        "state": farm.state,
                        "location": _point(farm.lat, farm.lng),
                    },
                ).json()
            )
            summary.farms += 1
            if not existing.get(farm.name):
                for j, item in enumerate(farm.listings):
                    if item.crop_id not in crops:
                        raise SeedError(f"crop '{item.crop_id}' is not in the catalog crop list")
                    gw.call(
                        "POST",
                        "/catalog/listings",
                        token=token,
                        idempotency_key=f"seed-listing-{f_idx:04d}-{k}-{j}",
                        json={
                            "farmId": made["id"],
                            "cropId": item.crop_id,
                            "variety": item.variety,
                            "description": item.description,
                            "qtyAvailable": item.qty_available,
                            "unit": item.unit,
                            "pricePerUnit": item.price_per_unit,
                            "harvestDate": item.harvest_date,
                            "isOrganic": item.is_organic,
                        },
                    )
                    summary.listings += 1

    for customer in data.customers:
        _ensure_user(gw, customer.name, customer.phone, ["customer"], customer.lat, customer.lng)
        summary.customers += 1
    return summary


def as_dict(summary: Summary) -> dict:
    return asdict(summary)
