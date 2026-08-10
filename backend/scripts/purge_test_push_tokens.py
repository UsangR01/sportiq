"""Clear push tokens the test suite wrote into the dev database.

The suite runs against the DEV database (a tracked gap of its own), and every test that
exercises PUT /user/push-token persists its fixture token against a real user row. 164 users
now share ExponentPushToken[abcDEF123] and one holds ExponentPushToken[dead]. A broad
notify_users run would fire 165 sends that can only fail, burning Expo's unauthenticated rate
limit and filling the logs with DeviceNotRegistered for devices that never existed.

DELIBERATELY AN ALLOW-LIST OF KNOWN-FAKE VALUES, NOT A PATTERN MATCH. A real Expo token and a
fixture token are the same shape -- ExponentPushToken[...] -- so anything heuristic risks
clearing the one genuine device registration in this database, which was registered by hand on
a real phone and cannot be recreated from here. Deleting a real token silently disables that
user's notifications; leaving a fake one costs a failed send. The asymmetry decides it.

Dry-run by default, --confirm to execute, mirroring scripts/purge_tennis_test_pollution.py.
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlalchemy import func, select, update  # noqa: E402

from app.core.database import async_session_factory  # noqa: E402
from app.users.models import User  # noqa: E402

# Literals that appear in backend/tests/. Extend this list rather than reaching for a regex.
KNOWN_TEST_TOKENS = [
    "ExponentPushToken[abcDEF123]",
    "ExponentPushToken[dead]",
]


async def main(confirm: bool) -> None:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(User.expo_push_token, func.count())
                .where(User.expo_push_token.isnot(None))
                .group_by(User.expo_push_token)
            )
        ).all()

        doomed = sum(count for token, count in rows if token in KNOWN_TEST_TOKENS)
        kept = [(token, count) for token, count in rows if token not in KNOWN_TEST_TOKENS]

        print(f"  users holding a push token: {sum(c for _, c in rows)}")
        print(f"  would clear (known test tokens): {doomed}")
        for token, count in kept:
            print(f"  KEEPING {count}x {token[:24]}... (not a known test value)")

        if not confirm:
            print("\n  dry run — re-run with --confirm to apply")
            return

        result = await db.execute(
            update(User)
            .where(User.expo_push_token.in_(KNOWN_TEST_TOKENS))
            .values(expo_push_token=None)
        )
        await db.commit()
        print(f"\n  cleared {result.rowcount} test push tokens")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually apply the update")
    asyncio.run(main(parser.parse_args().confirm))
