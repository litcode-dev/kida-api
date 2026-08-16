import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from app.main import app
from app.database import Base, get_db
from app.middleware.auth_middleware import get_redis

TEST_DB_URL = "postgresql+asyncpg://litmusic:litmusic@localhost:5432/litmusic_test"


class FakeRedis:
    """Minimal in-memory stand-in for redis.asyncio.Redis used in tests.

    Stateful so flows that write then read a key (verification codes, refresh
    tokens) behave like real Redis. Keys with a TTL are not expired in tests.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        # Sets live apart from strings, as they do in Redis — the refresh-token
        # index is a set while the tokens themselves are strings.
        self.sets: dict[str, set[str]] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        removed = 0
        for k in keys:
            if self.store.pop(k, None) is not None:
                removed += 1
            if self.sets.pop(k, None) is not None:
                removed += 1
        return removed

    async def exists(self, key):
        return 1 if key in self.store or key in self.sets else 0

    async def sadd(self, key, *members):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(members)
        return len(bucket) - before

    async def srem(self, key, *members):
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = len(bucket & set(members))
        bucket -= set(members)
        if not bucket:
            self.sets.pop(key, None)
        return removed

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def expire(self, key, ttl):
        # TTLs are not simulated; keys live for the test.
        return 1 if key in self.store or key in self.sets else 0

    async def ping(self):
        return True

    async def aclose(self):
        return None

test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def setup_db():
    try:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    except Exception:
        pytest.skip("Test database unavailable")


@pytest_asyncio.fixture
async def db_session(setup_db):
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    """Rate limiters use process-global in-memory counters that would otherwise
    bleed across tests hitting the same endpoint from the same client IP."""
    from app.middleware.rate_limit import limiter, ai_limiter
    prev = (limiter.enabled, ai_limiter.enabled)
    limiter.enabled = False
    ai_limiter.enabled = False
    yield
    limiter.enabled, ai_limiter.enabled = prev


@pytest_asyncio.fixture
async def fake_redis():
    return FakeRedis()


@pytest_asyncio.fixture
async def client(db_session, fake_redis):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: fake_redis

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
