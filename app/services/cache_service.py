"""Simple Redis-backed cache service.

Keys are namespaced under ``litmusic:cache:`` to avoid clashing with auth
refresh tokens or Celery data stored in the same Redis instance.
"""
import json
from redis.asyncio import Redis
from app.config import get_settings

# TTLs
TTL_DRONE_CATEGORIES = 3600   # 1 hour  – categories change infrequently
TTL_DRONE_TITLES = 300        # 5 minutes
TTL_DRONE_LIST = 300          # 5 minutes — invalidated explicitly on writes
TTL_DRUM_KIT_LIST = 300       # 5 minutes
TTL_DRUM_KIT_DETAIL = 600     # 10 minutes

_PREFIX = "litmusic:cache:"


def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def get(key: str) -> dict | list | None:
    async with _redis() as r:
        raw = await r.get(f"{_PREFIX}{key}")
    return json.loads(raw) if raw else None


async def set(key: str, value: dict | list, ttl: int) -> None:
    async with _redis() as r:
        await r.set(f"{_PREFIX}{key}", json.dumps(value), ex=ttl)


async def delete(key: str) -> None:
    async with _redis() as r:
        await r.delete(f"{_PREFIX}{key}")


async def delete_pattern(pattern: str) -> None:
    """Delete all keys matching a glob pattern (e.g. 'drum_kit:list:*')."""
    async with _redis() as r:
        keys = await r.keys(f"{_PREFIX}{pattern}")
        if keys:
            await r.delete(*keys)
