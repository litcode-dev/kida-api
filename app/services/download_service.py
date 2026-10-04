import uuid
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.download import Download
from app.models.loop import Loop
from app.services.loop_service import like_count_expr


async def get_user_download_history(
    db: AsyncSession,
    user_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list, int]:
    """Return distinct loops downloaded by the user, newest first."""
    subq = (
        select(
            Download.loop_id,
            func.max(Download.downloaded_at).label("last_downloaded_at"),
            func.count(Download.id).label("times_downloaded"),
        )
        .where(Download.user_id == user_id, Download.loop_id.isnot(None))
        .group_by(Download.loop_id)
        .subquery()
    )

    total = await db.scalar(select(func.count()).select_from(subq))

    q = (
        select(
            Loop.id.label("loop_id"),
            Loop.title,
            Loop.slug,
            Loop.genre,
            Loop.bpm,
            Loop.key,
            Loop.duration,
            Loop.tempo_feel,
            Loop.price,
            Loop.is_free,
            Loop.thumbnail_s3_key,
            subq.c.last_downloaded_at,
            subq.c.times_downloaded,
            like_count_expr().label("like_count"),
        )
        .join(subq, Loop.id == subq.c.loop_id)
        .order_by(subq.c.last_downloaded_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(q)
    return result.mappings().all(), total or 0
