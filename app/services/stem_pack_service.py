import uuid
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from app.models.stem_pack import StemPack, Stem
from app.models.purchase import Purchase
from app.models.user import User
from app.schemas.stem_pack import StemPackCreate, StemCreate
from app.exceptions import NotFoundError, EntitlementError
from app.services import s3_service, encryption_service
from app.utils.audio_validator import validate_wav_upload
from app.utils.ffmpeg_helpers import generate_preview_mp3
from fastapi import UploadFile


def _slugify(title: str, uid: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return f"{slug}-{uid[:8]}"


async def create_stem_pack(db: AsyncSession, data: StemPackCreate, created_by: uuid.UUID) -> StemPack:
    pack_id = uuid.uuid4()
    pack = StemPack(
        id=pack_id,
        title=data.title,
        slug=_slugify(data.title, str(pack_id)),
        loop_id=data.loop_id,
        genre=data.genre,
        bpm=data.bpm,
        key=data.key,
        tags=data.tags,
        price=data.price,
        description=data.description,
        created_by=created_by,
    )
    db.add(pack)
    await db.commit()
    await db.refresh(pack)
    return pack


async def add_stem_to_pack(
    db: AsyncSession,
    pack_id: uuid.UUID,
    file: UploadFile,
    data: StemCreate,
) -> Stem:
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")

    wav_bytes = await validate_wav_upload(file)
    preview_mp3 = generate_preview_mp3(wav_bytes)
    aes_key, aes_iv = encryption_service.generate_key_and_iv()
    encrypted_wav = encryption_service.encrypt_bytes(wav_bytes, aes_key, aes_iv)

    stem_id = str(uuid.uuid4())
    enc_key = s3_service.s3_key_for_encrypted_stem(stem_id)
    prev_key = s3_service.s3_key_for_stem_preview(stem_id)
    await s3_service.upload_bytes(enc_key, encrypted_wav)
    await s3_service.upload_bytes(prev_key, preview_mp3, "audio/mpeg")

    stem = Stem(
        id=uuid.UUID(stem_id),
        stem_pack_id=pack_id,
        label=data.label,
        file_s3_key=enc_key,
        preview_s3_key=prev_key,
        aes_key=aes_key,
        aes_iv=aes_iv,
        duration=data.duration,
    )
    db.add(stem)
    await db.commit()
    await db.refresh(stem)
    return stem


async def list_stem_packs(
    db: AsyncSession, created_by: uuid.UUID, page: int = 1, page_size: int = 20
) -> tuple[list[StemPack], int]:
    q = select(StemPack).where(StemPack.created_by == created_by)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    q = (
        q.options(selectinload(StemPack.stems))
        .order_by(StemPack.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.scalars(q)
    return list(result.all()), total or 0


async def get_stem_pack_with_stems(db: AsyncSession, pack_id: uuid.UUID) -> StemPack:
    result = await db.scalar(
        select(StemPack)
        .options(selectinload(StemPack.stems))
        .where(StemPack.id == pack_id)
    )
    if not result:
        raise NotFoundError("StemPack not found")
    return result


async def check_stem_pack_entitlement(db: AsyncSession, user: User, pack_id: uuid.UUID) -> None:
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.stem_pack_id == pack_id,
        )
    )
    if not purchase:
        raise EntitlementError("Purchase this stem pack to download")
