import asyncio
import hashlib
import boto3
import structlog
from botocore.config import Config
from app.config import get_settings

log = structlog.get_logger()
settings = get_settings()

# Cloudflare R2 validates the SigV4 credential scope against the region "auto"
# and rejects a real AWS region name. Other S3-compatible stores (MinIO,
# LocalStack) ignore the region, so only R2 is special-cased here.
_R2_HOST = "r2.cloudflarestorage.com"


def _signing_region(endpoint_url: str) -> str:
    return "auto" if _R2_HOST in endpoint_url else settings.aws_region


def build_content_client():
    """A client for the bucket the catalogue lives in, wherever that is.

    S3_ENDPOINT_URL was accepted in the environment but never passed to boto3,
    so every request went to AWS whatever it said — which answers a Cloudflare
    R2 key with "InvalidAccessKeyId: The AWS Access Key Id you provided does
    not exist in our records" and looks like a credentials problem rather than
    a request sent to the wrong company.

    The API and the Celery tasks both build their client here, so moving the
    bucket moves all of them at once: a worker still writing to the old store
    while the API reads the new one leaves uploads stuck in "processing"
    forever.
    """
    endpoint_url = settings.s3_endpoint_url.strip()
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=_signing_region(endpoint_url),
        # Explicit rather than inherited: an S3-compatible store accepts SigV4
        # and nothing older.
        config=Config(signature_version="s3v4"),
    )


# boto3 clients are thread-safe — reuse one instance to avoid per-call construction overhead
_client = build_content_client()

# Cloudflare R2 is S3-compatible — a separate client pointed at the R2 endpoint,
# used for the desktop app installers (the rest of the content lives on AWS S3).
# Constructed lazily so the app still boots when R2 isn't configured.
_r2_client = None


def _get_r2_client():
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _r2_client


async def generate_r2_presigned_url(key: str, expiry_seconds: int = 900) -> str:
    """Generate a pre-signed GET URL for an object in the Cloudflare R2 bucket."""
    def _presign():
        return _get_r2_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.r2_bucket_name, "Key": key},
            ExpiresIn=expiry_seconds,
        )

    return await asyncio.to_thread(_presign)


async def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to S3 without blocking the event loop. Returns the S3 key."""
    def _upload():
        _client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    return await asyncio.to_thread(_upload)


async def generate_presigned_url(key: str, expiry_seconds: int = 900) -> str:
    """Generate a pre-signed GET URL valid for expiry_seconds (default 15 min)."""
    def _presign():
        return _client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket_name, "Key": key},
            ExpiresIn=expiry_seconds,
        )

    return await asyncio.to_thread(_presign)


async def get_download_url(key: str, expiry_seconds: int = 900) -> str:
    """Return a download URL for a key.

    Uses a plain CloudFront URL when S3_CLOUDFRONT_URL is configured
    (files are AES-encrypted, so no additional signing is needed).
    Falls back to an S3 presigned URL otherwise.
    """
    if settings.s3_cloudfront_url:
        base = settings.s3_cloudfront_url.rstrip("/")
        return f"{base}/{key}"
    return await generate_presigned_url(key, expiry_seconds)


async def delete_object(key: str) -> None:
    def _delete():
        _client.delete_object(Bucket=settings.s3_bucket_name, Key=key)

    await asyncio.to_thread(_delete)


async def delete_objects_after_commit(keys: list[str]) -> None:
    """Remove assets a committed row no longer refers to.

    Replacing an asset has to delete the old object *after* the row that names
    the new one is committed: deleting first and then rolling back — a later
    validator refusing the same request is enough — leaves the row pointing at
    an object that no longer exists, which no retry repairs.

    A failure here is logged rather than raised. The write it belongs to has
    already succeeded, so turning a leftover object into a 500 would tell the
    caller their update was lost and invite a duplicate; an orphan costs
    storage and nothing else.
    """
    for key in keys:
        try:
            await delete_object(key)
        except Exception as exc:  # noqa: BLE001 - an orphan beats a failed update
            log.warning("s3_stale_object_not_deleted", key=key, error=str(exc))


def s3_key_for_raw_loop(loop_id: str) -> str:
    return f"loops/raw/{loop_id}.wav"


def s3_key_for_raw_drone(drone_id: str) -> str:
    return f"drones/raw/{drone_id}.wav"


def s3_key_for_encrypted_loop(loop_id: str) -> str:
    return f"loops/encrypted/{loop_id}.wav.enc"


def s3_key_for_loop_preview(loop_id: str) -> str:
    return f"previews/{loop_id}_preview.mp3"


def content_digest(data: bytes) -> str:
    """Short hash of an asset's bytes, used as part of its key.

    Thumbnail keys used to be derived from the item id and content type alone,
    so replacing a JPEG with another JPEG produced the identical key and the
    identical CloudFront URL — the new image sat in S3 while the CDN and the
    app both went on serving the cached old one. Mixing the content in means a
    different image is always a different URL, with no invalidation needed.
    """
    return hashlib.sha256(data).hexdigest()[:12]


def _thumbnail_key(prefix: str, item_id: str, ext: str, digest: str | None) -> str:
    # Without a digest this returns the historical key, so rows written before
    # content-addressed keys still resolve.
    suffix = f"_{digest}" if digest else ""
    return f"{prefix}/{item_id}_thumbnail{suffix}.{ext}"


def s3_key_for_loop_thumbnail(loop_id: str, ext: str = "jpg", digest: str | None = None) -> str:
    return _thumbnail_key("thumbnails", loop_id, ext, digest)


def s3_key_for_raw_stem(stem_id: str) -> str:
    return f"stems/raw/{stem_id}.wav"


def s3_key_for_encrypted_stem(stem_id: str) -> str:
    return f"stems/encrypted/{stem_id}.wav.enc"


def s3_key_for_stem_preview(stem_id: str) -> str:
    return f"stems/previews/{stem_id}_preview.mp3"


def s3_key_for_encrypted_arrangement_track(track_id: str) -> str:
    return f"stems/arrangements/encrypted/{track_id}.wav.enc"


def s3_key_for_arrangement_track_preview(track_id: str) -> str:
    return f"stems/arrangements/previews/{track_id}_preview.mp3"


def s3_key_for_encrypted_drone(drone_id: str) -> str:
    return f"drones/encrypted/{drone_id}.wav.enc"


def s3_key_for_drone_preview(drone_id: str) -> str:
    return f"drones/previews/{drone_id}_preview.mp3"


def s3_key_for_drone_thumbnail(drone_id: str, ext: str = "jpg", digest: str | None = None) -> str:
    return _thumbnail_key("drones/thumbnails", drone_id, ext, digest)


def s3_key_for_raw_drum_sample(sample_id: str) -> str:
    return f"drum-kits/raw/{sample_id}.wav"


def s3_key_for_encrypted_drum_sample(sample_id: str) -> str:
    return f"drum-kits/encrypted/{sample_id}.wav.enc"


def s3_key_for_drum_sample_preview(sample_id: str) -> str:
    return f"drum-kits/previews/{sample_id}_preview.mp3"


def s3_key_for_drum_kit_thumbnail(kit_id: str, ext: str = "jpg", digest: str | None = None) -> str:
    return _thumbnail_key("drum-kits/thumbnails", kit_id, ext, digest)
