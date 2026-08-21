import uuid
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, or_, select
from app.models.loop import Genre, Loop
from app.models.purchase import Purchase
from app.models.user import User
from app.schemas.loop import LoopCreate, LoopUpdate, LoopFilter
from app.exceptions import AppError, NotFoundError, EntitlementError
from app.services import price_sync_service, s3_service
from app.utils.audio_validator import validate_wav_upload
from fastapi import UploadFile


def _slugify(title: str, uid: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return f"{slug}-{uid[:8]}"


async def create_loop(
    db: AsyncSession,
    file: UploadFile,
    data: LoopCreate,
    created_by: uuid.UUID,
    thumbnail: UploadFile | None = None,
) -> Loop:
    wav_bytes = await validate_wav_upload(file)

    loop_id = str(uuid.uuid4())
    raw_key = s3_service.s3_key_for_raw_loop(loop_id)
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    thumb_key = None
    if thumbnail:
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        thumb_key = s3_service.s3_key_for_loop_thumbnail(
            loop_id, ext, s3_service.content_digest(thumb_bytes)
        )
        await s3_service.upload_bytes(thumb_key, thumb_bytes, content_type)

    loop = Loop(
        id=uuid.UUID(loop_id),
        title=data.title,
        slug=_slugify(data.title, loop_id),
        genre=data.genre,
        bpm=data.bpm,
        time_signature=data.time_signature,
        duration=0,
        tempo_feel=data.tempo_feel,
        tags=data.tags,
        price=data.price,
        is_free=data.is_free,
        is_paid=not data.is_free,
        desired_price_usd=data.desired_price_usd,
        thumbnail_s3_key=thumb_key,
        created_by=created_by,
        status="processing",
    )
    if not loop.is_free:
        price_sync_service.ensure_sku(loop, "loop")
    db.add(loop)
    await db.commit()
    await db.refresh(loop)
    return loop


async def get_loop(db: AsyncSession, loop_id: uuid.UUID) -> Loop:
    loop = await db.get(Loop, loop_id)
    if not loop:
        raise NotFoundError(f"Loop {loop_id} not found")
    return loop


def _search_clause(query: str):
    """Free text over the fields a person would search by.

    Title, description and genre, ORed — searching "worship" should surface a
    worship-genre loop whose title never says the word, and a loop whose
    description mentions it. The dedicated `genre` parameter still exists for
    exact filtering and ANDs with this.

    Genre is matched by resolving the term to enum members first, so the
    comparison runs against the display value ("Afrobeat Worship") rather than
    the stored label, and against an indexed equality rather than a cast.
    """
    like = f"%{query}%"
    clauses = [Loop.title.ilike(like), Loop.description.ilike(like)]
    genres = Genre.matching(query)
    if genres:
        clauses.append(Loop.genre.in_(genres))
    return or_(*clauses)


async def list_loops(db: AsyncSession, filters: LoopFilter) -> tuple[list[Loop], int]:
    q = select(Loop)
    if filters.ready_only:
        # A loop is browsable only once its audio is on S3. Before that,
        # preview and download both refuse it with a 409, so listing it puts
        # something in the catalogue that cannot be played or bought.
        q = q.where(Loop.status == "ready")
    if filters.created_by:
        q = q.where(Loop.created_by == filters.created_by)
    if filters.search:
        q = q.where(_search_clause(filters.search))
    if filters.genre:
        q = q.where(Loop.genre == filters.genre)
    if filters.bpm_min is not None:
        q = q.where(Loop.bpm >= filters.bpm_min)
    if filters.bpm_max is not None:
        q = q.where(Loop.bpm <= filters.bpm_max)
    if filters.key:
        q = q.where(Loop.key.ilike(f"%{filters.key}%"))
    if filters.time_signature:
        q = q.where(Loop.time_signature.in_(filters.time_signature))
    if filters.tempo_feel:
        q = q.where(Loop.tempo_feel == filters.tempo_feel)
    if filters.is_free is not None:
        q = q.where(Loop.is_free == filters.is_free)
    if filters.tags:
        q = q.where(Loop.tags.overlap(filters.tags))

    count_q = select(func.count()).select_from(q.subquery())
    total = await db.scalar(count_q)

    sort_map = {
        "newest": Loop.created_at.desc(),
        "most_downloaded": Loop.download_count.desc(),
        "most_played": Loop.play_count.desc(),
    }
    q = q.order_by(sort_map.get(filters.sort, Loop.created_at.desc()))
    q = q.offset((filters.page - 1) * filters.page_size).limit(filters.page_size)
    result = await db.scalars(q)
    return list(result.all()), total or 0


async def increment_play_count(db: AsyncSession, loop_id: uuid.UUID) -> None:
    loop = await get_loop(db, loop_id)
    loop.play_count += 1
    await db.commit()


def assert_loop_ready(loop: Loop) -> None:
    """Refuse to serve a loop whose audio is not on S3 yet.

    Replacing a loop's audio (see update_loop) clears file_s3_key/preview_s3_key
    and flips status to "processing" until the Celery job re-encrypts the upload.
    Without this guard the download endpoint happily signs a URL for a NULL key —
    the client gets a 200 pointing at ".../None" with null aes_key/aes_iv, which
    fails on the device long after the request looked successful.
    """
    if loop.status != "ready" or not loop.file_s3_key:
        raise AppError(
            "This loop is still being processed and cannot be downloaded yet",
            status_code=409,
            data={"status": loop.status},
        )


async def check_download_entitlement(
    db: AsyncSession, user: User, loop: Loop
) -> None:
    if loop.is_free:
        return
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.loop_id == loop.id,
        )
    )
    if not purchase:
        raise EntitlementError()


async def update_loop(
    db: AsyncSession,
    loop_id: uuid.UUID,
    data: LoopUpdate,
    thumbnail: UploadFile | None = None,
    file: UploadFile | None = None,
) -> tuple[Loop, bool]:
    loop = await get_loop(db, loop_id)

    if thumbnail:
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_loop_thumbnail(
            str(loop_id), ext, s3_service.content_digest(thumb_bytes)
        )
        old_thumb_key = loop.thumbnail_s3_key
        # Upload first: deleting first left the row pointing at a missing object
        # if the upload then failed. Re-uploading identical bytes lands on the
        # same key, so only remove the old one when it really is a different key.
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        loop.thumbnail_s3_key = new_thumb_key
        if old_thumb_key and old_thumb_key != new_thumb_key:
            await s3_service.delete_object(old_thumb_key)

    should_reprocess = False
    if file:
        wav_bytes = await validate_wav_upload(file)
        if loop.file_s3_key:
            await s3_service.delete_object(loop.file_s3_key)
        if loop.preview_s3_key:
            await s3_service.delete_object(loop.preview_s3_key)
        raw_key = s3_service.s3_key_for_raw_loop(str(loop_id))
        await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")
        loop.file_s3_key = None
        loop.preview_s3_key = None
        loop.status = "processing"
        should_reprocess = True

    update_fields = data.model_dump(exclude_none=True)
    new_desired = update_fields.pop("desired_price_usd", None)
    for field, value in update_fields.items():
        setattr(loop, field, value)

    # create_loop keeps is_paid as the inverse of is_free; the update path has to
    # do the same or a loop switched to free keeps reporting is_paid=true (and
    # vice versa), which clients use to decide whether to gate the download.
    if "is_free" in update_fields:
        loop.is_paid = not loop.is_free

    if new_desired is not None and new_desired != loop.desired_price_usd:
        loop.desired_price_usd = new_desired
        price_sync_service.mark_price_dirty(loop)
        if not loop.is_free:
            price_sync_service.ensure_sku(loop, "loop")

    await db.commit()
    await db.refresh(loop)
    return loop, should_reprocess


async def delete_loop(db: AsyncSession, loop_id: uuid.UUID) -> None:
    loop = await get_loop(db, loop_id)
    keys_to_delete = [
        s3_service.s3_key_for_raw_loop(str(loop_id)),
        loop.file_s3_key,
        loop.preview_s3_key,
        loop.thumbnail_s3_key,
    ]
    for key in keys_to_delete:
        if key:
            await s3_service.delete_object(key)
    await db.delete(loop)
    await db.commit()
