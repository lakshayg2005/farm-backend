"""Admin command line, e.g. to create the first admin (no one can sign up as admin via the API):

    uv run --package farm-identity python -m identity_service.cli create-admin \
        --name "Platform Admin" --phone +919999999999 --password "a-strong-password"
"""

import argparse
import asyncio

from farm_common.auth import Role
from identity_service.database import get_session_factory
from identity_service.db import User
from identity_service.schemas import normalize_phone
from identity_service.security import hash_password
from identity_service.service import find_by_phone, publish, user_payload


async def create_admin(name: str, phone: str, password: str) -> str:
    phone = normalize_phone(phone)
    async with get_session_factory()() as session:
        user = await find_by_phone(session, phone)
        if user is None:
            user = User(
                name=name,
                phone=phone,
                password_hash=hash_password(password),
                phone_verified=True,
                role_rows=[],
            )
            session.add(user)
            await session.flush()
            publish(session, "user.registered", user_payload(user))
        user.add_role(Role.ADMIN)
        publish(session, "user.roles_changed", user_payload(user))
        await session.commit()
        return str(user.id)


def main() -> None:
    parser = argparse.ArgumentParser(prog="identity_service.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create a user with the admin role")
    admin.add_argument("--name", required=True)
    admin.add_argument("--phone", required=True)
    admin.add_argument("--password", required=True)
    args = parser.parse_args()

    if args.command == "create-admin":
        if len(args.password) < 8:
            parser.error("password must be at least 8 characters")
        user_id = asyncio.run(create_admin(args.name, args.phone, args.password))
        print(f"admin ready: {user_id}")


if __name__ == "__main__":
    main()
