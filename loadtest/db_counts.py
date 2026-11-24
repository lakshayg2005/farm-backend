"""Row counts of every table in the service schemas: snapshot before a test, compare after cleanup.

    uv run python loadtest/db_counts.py snapshot before.json
    uv run python loadtest/db_counts.py compare before.json

Outbox and processed-event tables are listed separately: they grow with any event, including
real users' activity during the test, so only the business tables are expected to match exactly.
"""

import asyncio
import json
import os
import sys

import asyncpg

SCHEMAS = ("identity", "catalog", "land", "media", "order", "logistics", "notification")
EVENT_TABLES = ("outbox", "processed_events")


async def counts() -> dict[str, int]:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    tables = await conn.fetch(
        "select table_schema, table_name from information_schema.tables"
        " where table_schema = any($1) and table_type = 'BASE TABLE'"
        " and table_name <> 'alembic_version' order by 1, 2",
        list(SCHEMAS),
    )
    out = {}
    for t in tables:
        name = f"{t['table_schema']}.{t['table_name']}"
        out[name] = await conn.fetchval(
            f'select count(*) from "{t["table_schema"]}"."{t["table_name"]}"'
        )
    await conn.close()
    return out


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("snapshot", "compare"):
        sys.exit(__doc__)
    now = asyncio.run(counts())
    if sys.argv[1] == "snapshot":
        json.dump(now, open(sys.argv[2], "w"), indent=1, sort_keys=True)
        print(f"saved {len(now)} tables to {sys.argv[2]}")
        return
    before = json.load(open(sys.argv[2]))
    changed = False
    for name in sorted(set(before) | set(now)):
        a, b = before.get(name), now.get(name)
        if a != b:
            kind = "events" if name.split(".")[1] in EVENT_TABLES else "DATA"
            changed |= kind == "DATA"
            print(f"{kind:6s} {name:40s} {a} -> {b}")
    print(
        "business tables match the snapshot"
        if not changed
        else "business tables differ (see DATA lines)"
    )


if __name__ == "__main__":
    main()
