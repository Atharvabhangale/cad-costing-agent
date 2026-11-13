"""Create the first admin user and print their API token, once.

Only needed when auth is turned on (CAD_COSTING_AUTH_ENABLED=1). There is no
default user, password or token anywhere in the application: this script is the
bootstrap path, and it refuses once an active admin exists — further users are
created by an admin through `POST /auth/users`.

Works on whatever database CAD_COSTING_DATABASE_URL points at (default: the
local SQLite file), migrating and seeding it first exactly as app startup does.

    python scripts/create_admin.py --name "Asha Rao"

The token is printed once and only a salted hash of it is stored. This is a
proof-of-concept identity mechanism, not SSO/OIDC and not production-grade.
"""

import argparse
import sys

from sqlalchemy.orm import sessionmaker

from cad_costing_agent.db import database_url, get_engine, init_db
from cad_costing_agent.repository import UserRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="The admin's name, as the audit log will show it.")
    args = parser.parse_args(argv)

    engine = get_engine()
    init_db(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        repository = UserRepository(session)
        if repository.active_admin_count() > 0:
            print(
                "An active admin already exists. Create further users as that admin through "
                "POST /auth/users; this script only bootstraps the first one.",
                file=sys.stderr,
            )
            return 1
        user, token = repository.create(name=args.name, role="admin", created_by="bootstrap")
        session.commit()

    print(f"Created admin {user.name} ({user.user_id}) in {database_url()}")
    print("API token (shown once; it is not stored and cannot be recovered):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
