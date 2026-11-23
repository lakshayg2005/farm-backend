"""Delete everything a load test or journey run created, and nothing else.

Test people are found by phone prefix (load tests: +917000...) and/or exact user ids (journey runs
print theirs). From them the script follows every table that points at a user, a farm, a listing,
a plot, a reservation, an order, a pool or a media file, across all service schemas, plus the
outbox / processed-event rows whose payload mentions one of those ids, and the photo files in
object storage.

Dry run by default (prints what it would delete). Run on a machine that can reach Postgres as the
database owner, e.g. the VM:

    uv run python loadtest/cleanup.py --phone-prefix +917000            # dry run
    uv run python loadtest/cleanup.py --phone-prefix +917000 --apply    # delete
    uv run python loadtest/cleanup.py --user-ids-file journey-users.txt --apply

Environment: DATABASE_URL (postgresql://owner:pass@host:5432/farm), and for photos S3_ENDPOINT,
S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET (default farm-media).
"""

import argparse
import asyncio
import os
import re
import sys
from uuid import UUID

import asyncpg

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SCHEMAS = ["identity", "catalog", "land", "media", "order", "logistics", "notification"]


async def ids(conn: asyncpg.Connection, sql: str, *args) -> set[UUID]:
    return {r[0] for r in await conn.fetch(sql, *args) if r[0] is not None}


async def collect(conn: asyncpg.Connection, users: set[UUID]) -> dict[str, set[UUID]]:
    u = list(users)
    c: dict[str, set[UUID]] = {"users": users}
    c["farms"] = await ids(conn, "select id from catalog.farms where farmer_id = any($1)", u)
    c["listings"] = await ids(
        conn,
        "select id from catalog.produce_listings where farmer_id = any($1) or farm_id = any($2)",
        u,
        list(c["farms"]),
    )
    c["plots"] = await ids(conn, "select id from land.plots where farmer_id = any($1)", u)
    c["seasons"] = await ids(
        conn, "select id from land.plot_seasons where plot_id = any($1)", list(c["plots"])
    )
    c["reservations"] = await ids(
        conn,
        "select id from land.reservations where customer_id = any($1) or farmer_id = any($1)"
        " or plot_season_id = any($2)",
        u,
        list(c["seasons"]),
    )
    c["orders"] = await ids(
        conn,
        'select id from "order".orders where customer_id = any($1) or farm_owner_id = any($1)'
        " or farm_id = any($2) or reservation_id = any($3)",
        u,
        list(c["farms"]),
        list(c["reservations"]),
    )
    c["checkouts"] = await ids(
        conn,
        'select id from "order".checkouts where customer_id = any($1)'
        ' or id in (select checkout_id from "order".orders where id = any($2))',
        u,
        list(c["orders"]),
    )
    # A pool is test data when one of its stops is a test order or a test partner has it.
    c["pools"] = await ids(
        conn,
        "select id from logistics.delivery_pools where partner_id = any($1)"
        " or id in (select pool_id from logistics.pool_stops where order_id = any($2))",
        u,
        list(c["orders"]),
    )
    real_stops = await conn.fetchval(
        "select count(*) from logistics.pool_stops where pool_id = any($1)"
        " and not (order_id = any($2))",
        list(c["pools"]),
        list(c["orders"]),
    )
    if real_stops:
        sys.exit(f"refusing: {real_stops} stops of real orders share a pool with test data")
    c["stops"] = await ids(
        conn, "select id from logistics.pool_stops where pool_id = any($1)", list(c["pools"])
    )
    owners = set().union(
        c["farms"], c["listings"], c["plots"], c["reservations"], c["orders"], c["stops"], users
    )
    c["media"] = await ids(
        conn,
        "select id from media.media where uploaded_by = any($1) or owner_id = any($2)",
        u,
        list(owners),
    )
    return c


def plan(c: dict[str, set[UUID]], phones: list[str]) -> list[tuple[str, str, list]]:
    """(label, delete statement, args): children first."""
    L = lambda k: list(c[k])  # noqa: E731
    return [
        (
            "notification.notifications",
            "delete from notification.notifications where user_id = any($1)",
            [L("users")],
        ),
        (
            "notification.device_tokens",
            "delete from notification.device_tokens where user_id = any($1)",
            [L("users")],
        ),
        (
            "logistics.location_pings",
            "delete from logistics.location_pings where partner_id = any($1) or pool_id = any($2)",
            [L("users"), L("pools")],
        ),
        (
            "logistics.pool_offers",
            "delete from logistics.pool_offers where partner_id = any($1) or pool_id = any($2)",
            [L("users"), L("pools")],
        ),
        (
            "logistics.pool_stops",
            "delete from logistics.pool_stops where pool_id = any($1)",
            [L("pools")],
        ),
        (
            "logistics.delivery_pools",
            "delete from logistics.delivery_pools where id = any($1)",
            [L("pools")],
        ),
        (
            "logistics.delivery_partners",
            "delete from logistics.delivery_partners where id = any($1) or phone = any($2)",
            [L("users"), phones],
        ),
        (
            "order.order_ratings",
            'delete from "order".order_ratings where order_id = any($1) or customer_id = any($2)',
            [L("orders"), L("users")],
        ),
        (
            "order.order_events",
            'delete from "order".order_events where order_id = any($1)',
            [L("orders")],
        ),
        (
            "order.order_items",
            'delete from "order".order_items where order_id = any($1)',
            [L("orders")],
        ),
        (
            "order.payments",
            'delete from "order".payments where customer_id = any($1) or order_id = any($2)',
            [L("users"), L("orders")],
        ),
        (
            "order.harvest_reservations",
            'delete from "order".harvest_reservations where customer_id = any($1) or farmer_id = any($1) or reservation_id = any($2)',
            [L("users"), L("reservations")],
        ),
        ("order.orders", 'delete from "order".orders where id = any($1)', [L("orders")]),
        ("order.checkouts", 'delete from "order".checkouts where id = any($1)', [L("checkouts")]),
        (
            "order.idempotency_keys",
            'delete from "order".idempotency_keys where customer_id = any($1)',
            [L("users")],
        ),
        (
            "catalog.stock_movements",
            "delete from catalog.stock_movements where listing_id = any($1) or order_id = any($2)",
            [L("listings"), L("orders")],
        ),
        (
            "catalog.stock_reservations",
            "delete from catalog.stock_reservations where order_id = any($1)",
            [L("orders")],
        ),
        (
            "catalog.produce_listings",
            "delete from catalog.produce_listings where id = any($1)",
            [L("listings")],
        ),
        ("catalog.farms", "delete from catalog.farms where id = any($1)", [L("farms")]),
        (
            "catalog.idempotency_keys",
            "delete from catalog.idempotency_keys where user_id = any($1)",
            [L("users")],
        ),
        (
            "land.crop_updates",
            "delete from land.crop_updates where reservation_id = any($1)",
            [L("reservations")],
        ),
        (
            "land.agreements",
            "delete from land.agreements where reservation_id = any($1) or plot_id = any($2)",
            [L("reservations"), L("plots")],
        ),
        (
            "land.reservations",
            "delete from land.reservations where id = any($1)",
            [L("reservations")],
        ),
        ("land.plot_seasons", "delete from land.plot_seasons where id = any($1)", [L("seasons")]),
        ("land.plots", "delete from land.plots where id = any($1)", [L("plots")]),
        ("media.media", "delete from media.media where id = any($1)", [L("media")]),
        (
            "media.idempotency_keys",
            "delete from media.idempotency_keys where user_id = any($1)",
            [L("users")],
        ),
        (
            "identity.refresh_tokens",
            "delete from identity.refresh_tokens where user_id = any($1)",
            [L("users")],
        ),
        (
            "identity.user_roles",
            "delete from identity.user_roles where user_id = any($1)",
            [L("users")],
        ),
        ("identity.otp_codes", "delete from identity.otp_codes where phone = any($1)", [phones]),
        ("identity.users", "delete from identity.users where id = any($1)", [L("users")]),
    ]


async def event_rows(conn: asyncpg.Connection, test_ids: set[str]) -> dict[str, list]:
    """Outbox rows (every schema) whose payload mentions a test id; their event ids."""
    found: dict[str, list] = {}
    for schema in SCHEMAS:
        key = "event_id" if schema == "notification" else "id"
        rows = await conn.fetch(f'select {key} as id, payload::text as p from "{schema}".outbox')
        hit = [r["id"] for r in rows if set(UUID_RE.findall(r["p"].lower())) & test_ids]
        found[schema] = hit
    return found


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phone-prefix", action="append", default=[])
    ap.add_argument("--user-ids-file", help="one user id per line")
    ap.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--keep-photos", action="store_true")
    args = ap.parse_args()
    if not args.phone_prefix and not args.user_ids_file:
        ap.error("give --phone-prefix and/or --user-ids-file")
    for p in args.phone_prefix:
        if not re.fullmatch(r"\+91\d{4,}", p):
            ap.error(f"phone prefix {p!r} is too broad: give at least +91 and 4 digits")

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    users: set[UUID] = set()
    for p in args.phone_prefix:
        users |= await ids(conn, "select id from identity.users where phone like $1", p + "%")
    if args.user_ids_file:
        with open(args.user_ids_file) as f:  # noqa: ASYNC230 - a tiny local file
            listed = [UUID(x.strip()) for x in f if x.strip()]
        users |= await ids(conn, "select id from identity.users where id = any($1)", listed)
    admins = await conn.fetchval(
        "select count(*) from identity.user_roles where user_id = any($1) and role = 'admin'",
        list(users),
    )
    if admins:
        sys.exit("refusing: the selection includes an admin account")
    phones = [
        r[0]
        for r in await conn.fetch(
            "select phone from identity.users where id = any($1)", list(users)
        )
    ]
    print(f"test users: {len(users)}")

    c = await collect(conn, users)
    for k, v in c.items():
        print(f"  {k:13s} {len(v)}")
    test_ids = {str(x) for v in c.values() for x in v}
    events = await event_rows(conn, test_ids)
    event_ids = [e for v in events.values() for e in v]
    print(f"  outbox rows   {len(event_ids)}")
    media_keys = [
        r
        for r in await conn.fetch(
            "select id, original_key from media.media where id = any($1)", list(c["media"])
        )
    ]

    if not args.apply:
        print("dry run: nothing deleted (add --apply)")
        await conn.close()
        return

    counts: dict[str, int] = {}
    async with conn.transaction():
        for label, sql, params in plan(c, phones):
            res = await conn.execute(sql, *params)
            counts[label] = int(res.split()[-1])
        for schema, hit in events.items():
            key = "event_id" if schema == "notification" else "id"
            res = await conn.execute(f'delete from "{schema}".outbox where {key} = any($1)', hit)
            counts[f"{schema}.outbox"] = int(res.split()[-1])
        for schema in SCHEMAS:
            res = await conn.execute(
                f'delete from "{schema}".processed_events where event_id = any($1)', event_ids
            )
            counts[f"{schema}.processed_events"] = int(res.split()[-1])
    await conn.close()
    for label, n in counts.items():
        if n:
            print(f"deleted {n:7d}  {label}")

    if media_keys and not args.keep_photos:
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        )
        bucket = os.environ.get("S3_BUCKET", "farm-media")
        keys = []
        for r in media_keys:
            if r["original_key"]:
                keys.append(r["original_key"])
            keys += [f"public/{r['id']}.jpg", f"thumbs/{r['id']}.jpg"]
        for i in range(0, len(keys), 1000):
            s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]}
            )
        print(f"deleted {len(keys)} photo objects (or their absent variants)")


if __name__ == "__main__":
    asyncio.run(main())
