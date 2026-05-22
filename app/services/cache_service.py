"""Simple Redis-backed cache service.

Keys are namespaced under ``litmusic:cache:`` to avoid clashing with auth
refresh tokens or Celery data stored in the same Redis instance.
"""
import asyncio
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

_LOCK_TTL = 10           # lock auto-expires (seconds) if the holder crashes
_LOCK_POLL_INTERVAL = 0.05   # 50 ms between polls
_LOCK_POLL_ATTEMPTS = 20     # wait up to 1 s total before direct fallback


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


async def get_or_set(key: str, fetch_fn, ttl: int) -> dict | list:
    """Cache-aside with a Redis lock to prevent stampede.

    Only one coroutine fetches the underlying data; all others poll until
    the value is populated. Falls back to a direct fetch if the lock holder
    crashes (lock TTL expires without the key being written).
    """
    full_key = f"{_PREFIX}{key}"
    lock_key = f"{_PREFIX}lock:{key}"

    async with _redis() as r:
        raw = await r.get(full_key)
        if raw is not None:
            return json.loads(raw)

        acquired = await r.set(lock_key, "1", nx=True, ex=_LOCK_TTL)

        if acquired:
            try:
                # Double-check: a concurrent winner may have just written the value
                raw = await r.get(full_key)
                if raw is not None:
                    return json.loads(raw)
                value = await fetch_fn()
                await r.set(full_key, json.dumps(value), ex=ttl)
                return value
            finally:
                await r.delete(lock_key)

        for _ in range(_LOCK_POLL_ATTEMPTS):
            await asyncio.sleep(_LOCK_POLL_INTERVAL)
            raw = await r.get(full_key)
            if raw is not None:
                return json.loads(raw)

        # Lock holder crashed; fetch directly as a last resort
        return await fetch_fn()
