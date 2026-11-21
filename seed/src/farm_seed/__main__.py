import argparse
import os

from farm_seed.client import Gateway
from farm_seed.data import build_dataset, build_v2_plan
from farm_seed.demo import run_demo
from farm_seed.ops import seed_operations
from farm_seed.v1 import as_dict, seed_v1
from farm_seed.v2 import seed_v2


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="farm_seed", description="Fill the platform with demo data"
    )
    parser.add_argument("--base-url", default=os.getenv("SEED_BASE_URL", "http://localhost/api/v1"))
    parser.add_argument("--admin-phone", default=os.getenv("SEED_ADMIN_PHONE"))
    parser.add_argument("--admin-password", default=os.getenv("SEED_ADMIN_PASSWORD"))
    parser.add_argument(
        "--profile",
        choices=("demo", "volume"),
        default="demo",
        help="demo: the small curated story (default). volume: generated data for load and geo tests",
    )
    parser.add_argument("--farmers", type=int, default=20)
    parser.add_argument("--customers", type=int, default=40)
    parser.add_argument(
        "--orders", type=int, default=200, help="produce orders to place (0 = none)"
    )
    parser.add_argument(
        "--skip-v2", action="store_true", help="only farmers, farms, listings, customers"
    )
    parser.add_argument(
        "--skip-ops",
        action="store_true",
        help="do not take orders through packing, inspection, pooling and delivery",
    )
    args = parser.parse_args()
    if not (args.admin_phone and args.admin_password):
        parser.error(
            "give --admin-phone and --admin-password (or SEED_ADMIN_PHONE / SEED_ADMIN_PASSWORD)"
        )
    gateway = Gateway(args.base_url)
    if args.profile == "demo":
        print("Seeded demo:", run_demo(gateway, args.admin_phone, args.admin_password))
        return
    data = build_dataset(args.farmers, args.customers)
    summary = seed_v1(gateway, data, args.admin_phone, args.admin_password)
    print("Seeded V1:", as_dict(summary))
    if not args.skip_v2:
        print("Seeded V2:", seed_v2(gateway, data, build_v2_plan(data, args.orders)))
        if not args.skip_ops and args.orders:
            print(
                "Seeded operations:",
                seed_operations(
                    gateway,
                    [f.phone for f in data.farmers],
                    args.admin_phone,
                    args.admin_password,
                ),
            )


if __name__ == "__main__":
    main()
