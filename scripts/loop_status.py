"""Why a newly uploaded loop is not in the catalogue — and the fix.

Users only see loops whose status is "ready". An upload starts "processing"
and becomes ready when the Celery worker's process_loop_upload job has
encrypted the audio and written the preview. So a loop missing from
GET /loops is almost always one of:

  * the worker is not running, so nothing is processed
  * the job was never queued — the API saved the loop, then failed to reach
    the broker (e.g. the kombu/redis-py mismatch), so the loop waits forever
    even after the worker is fixed
  * the job ran and failed every retry, leaving the loop "failed"

This prints whether a worker answers, the newest loops with their status, and
every loop stuck in processing or failed with whether its raw upload is still
in the bucket. With --requeue it queues process_loop_upload again for each
stuck loop whose raw upload is there; nothing is changed without it.

Run from the API or worker shell:
    python -m scripts.loop_status
    python -m scripts.loop_status --requeue
    python -m scripts.loop_status --requeue --include-failed
"""
import argparse
import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.models.loop import Loop
from app.services import s3_service


def _worker_status() -> str:
    from app.tasks.celery_app import celery_app

    try:
        replies = celery_app.control.ping(timeout=3)
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        return f"could not ask the broker: {exc}"
    if not replies:
        return "NO WORKER ANSWERED — uploads cannot be processed until one runs"
    return f"{len(replies)} worker(s) answered: " + ", ".join(
        name for reply in replies for name in reply
    )


async def _raw_present(loop: Loop) -> str:
    try:
        exists = await s3_service.object_exists(
            s3_service.s3_key_for_raw_loop(str(loop.id))
        )
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"
    return "yes" if exists else "no"


async def main(requeue: bool, include_failed: bool, newest: int) -> None:
    print(f"Worker: {_worker_status()}\n")

    async with AsyncSessionLocal() as db:
        latest = (await db.scalars(
            select(Loop).order_by(Loop.created_at.desc(), Loop.id.desc()).limit(newest)
        )).all()
        print(f"Newest {len(latest)} loops (only 'ready' ones are listed to users):")
        for loop in latest:
            print(f"  {loop.created_at:%Y-%m-%d %H:%M}  {loop.status:<10}  {loop.id}  {loop.title}")

        stuck_statuses = ["processing", "failed"] if include_failed else ["processing"]
        stuck = (await db.scalars(
            select(Loop)
            .where(Loop.status.in_(["processing", "failed"]))
            .order_by(Loop.created_at.desc())
        )).all()

    print(f"\nLoops not ready: {len(stuck)}")
    to_queue = []
    for loop in stuck:
        raw = await _raw_present(loop)
        print(f"  {loop.created_at:%Y-%m-%d %H:%M}  {loop.status:<10}  raw upload: {raw:<4}  {loop.id}  {loop.title}")
        if loop.status in stuck_statuses and raw == "yes":
            to_queue.append(loop)

    if not stuck:
        print("\nNothing is stuck. If a new loop is still missing, it is not in this database.")
    elif not requeue:
        print(
            f"\n{len(to_queue)} can be re-queued. Run again with --requeue"
            + ("" if include_failed else " (add --include-failed for failed ones)")
            + ". A loop whose raw upload is gone has to be uploaded again."
        )
    else:
        from app.tasks.upload_tasks import process_loop_upload

        for loop in to_queue:
            process_loop_upload.delay(str(loop.id))
        print(f"\nQueued {len(to_queue)} loop(s). Run again in a minute to watch them turn ready.")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requeue", action="store_true", help="queue processing again for stuck loops")
    parser.add_argument("--include-failed", action="store_true", help="also re-queue loops marked failed")
    parser.add_argument("--newest", type=int, default=10, help="how many recent loops to show")
    args = parser.parse_args()
    asyncio.run(main(args.requeue, args.include_failed, args.newest))
