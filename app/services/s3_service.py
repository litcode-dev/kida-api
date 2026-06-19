import asyncio
import boto3
from botocore.config import Config
from app.config import get_settings

settings = get_settings()

# boto3 clients are thread-safe — reuse one instance to avoid per-call construction overhead
_client = boto3.client(
    "s3",
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
    region_name=settings.aws_region,
)

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


def s3_key_for_raw_loop(loop_id: str) -> str:
    return f"loops/raw/{loop_id}.wav"


def s3_key_for_raw_drone(drone_id: str) -> str:
    return f"drones/raw/{drone_id}.wav"


def s3_key_for_encrypted_loop(loop_id: str) -> str:
    return f"loops/encrypted/{loop_id}.wav.enc"


def s3_key_for_loop_preview(loop_id: str) -> str:
    return f"previews/{loop_id}_preview.mp3"


def s3_key_for_loop_thumbnail(loop_id: str, ext: str = "jpg") -> str:
    return f"thumbnails/{loop_id}_thumbnail.{ext}"


def s3_key_for_encrypted_stem(stem_id: str) -> str:
    return f"stems/encrypted/{stem_id}.wav.enc"


def s3_key_for_stem_preview(stem_id: str) -> str:
    return f"stems/previews/{stem_id}_preview.mp3"


def s3_key_for_encrypted_drone(drone_id: str) -> str:
    return f"drones/encrypted/{drone_id}.wav.enc"


def s3_key_for_drone_preview(drone_id: str) -> str:
    return f"drones/previews/{drone_id}_preview.mp3"


def s3_key_for_drone_thumbnail(drone_id: str, ext: str = "jpg") -> str:
    return f"drones/thumbnails/{drone_id}_thumbnail.{ext}"


def s3_key_for_raw_drum_sample(sample_id: str) -> str:
    return f"drum-kits/raw/{sample_id}.wav"


def s3_key_for_encrypted_drum_sample(sample_id: str) -> str:
    return f"drum-kits/encrypted/{sample_id}.wav.enc"


def s3_key_for_drum_sample_preview(sample_id: str) -> str:
    return f"drum-kits/previews/{sample_id}_preview.mp3"


def s3_key_for_drum_kit_thumbnail(kit_id: str, ext: str = "jpg") -> str:
    return f"drum-kits/thumbnails/{kit_id}_thumbnail.{ext}"
