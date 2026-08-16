"""Finish the deletions left half-done by the old 30-day grace period.

One-off, for the deploy that removes the grace period. Accounts sitting in the
old pending state asked to be deleted and were waiting out a window that no
longer exists. They cannot simply be left: the migration that drops
``users.deleted_at`` would put them straight back into service.

Each one is run through ``auth_service.delete_user`` — the same path the app
now uses — rather than deleted in SQL, because the promise made to these people
covers more than this database: their stored files, their RevenueCat subscriber
record and their OneSignal push registration all have to go too, and only that
function knows how to reach them.

Reads the pending ids in raw SQL, since the model no longer has the column.

Run:
    python -m scripts.purge_pending_deletions --dry-run
    python -m scripts.purge_pending_deletions --yes
"""
import argparse
import asyncio
import uuid

from redis.asyncio import Redis
from sqlalchemy import text

from app.config import get_settings
from app.database import AsyncSessionLocal, engine
from app.models.user import User
from app.services import auth_service

_PENDING = text(
    "SELECT id, email, deleted_at FROM users "
    "WHERE deleted_at IS NOT NULL ORDER BY deleted_at"
)


async def _column_exists(db) -> bool:
    return bool(
        await db.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'deleted_at'"
            )
        )
    )


async def _run(dry_run: bool) -> int:
    async with AsyncSessionLocal() as db:
        if not await _column_exists(db):
            print("users.deleted_at is already gone — nothing to do.")
            return 0

        pending = (await db.execute(_PENDING)).all()
        if not pending:
            print("No accounts are pending deletion. Safe to migrate.")
            return 0

        print(f"{len(pending)} account(s) pending deletion:")
        for _id, email, deleted_at in pending:
            print(f"  {_id}  {email}  asked {deleted_at:%Y-%m-%d}")

        if dry_run:
            print("\n--dry-run: nothing deleted.")
            return 0

    # A separate session per account, so one failure cannot poison the rest.
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    deleted, failed = 0, 0
    try:
        for _id, email, _ in pending:
            async with AsyncSessionLocal() as db:
                user = await db.get(User, uuid.UUID(str(_id)))
                if user is None:  # already gone
                    continue
                try:
                    # The old flow recorded how the request arrived in Redis;
                    # that marker is not worth reviving for a one-off drain, so
                    # every one of these is attributed to the account holder.
                    await auth_service.delete_user(db, user, redis, None, actor="user")
                    deleted += 1
                    print(f"deleted {email}")
                except Exception as exc:  # noqa: BLE001 - report and carry on
                    await db.rollback()
                    failed += 1
                    print(f"FAILED  {email}: {exc}")
    finally:
        await redis.aclose()
        await engine.dispose()

    print(f"\ndone — {deleted} deleted, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="list the accounts and stop"
    )
    parser.add_argument(
        "--yes", action="store_true", help="required to actually delete anything"
    )
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        parser.error("this deletes accounts permanently — pass --yes, or --dry-run")

    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
