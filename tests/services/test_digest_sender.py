"""The daily mail's failure modes, which is where it kept getting stuck.

Two of these are the reason nothing arrived for weeks: a mail backend with no
credentials, which used to claim every item and report success while delivering
nothing, and a send where every message failed, which left the content stamped
as announced and therefore never mentioned again. Both now refuse to lose the
content, and both leave a row saying what happened.

The rest cover the lock: several things can start a digest now, and only one of
them may send it.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.digest_run import DigestRunStatus
from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.services import digest_sender, email_service
from app.services.auth_service import hash_password
from tests.conftest import TestSessionLocal


async def _user(db, email=None, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=email or f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Listener",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _ready_loop(db, user_id, title="Fresh Loop"):
    loop = Loop(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=100,
        duration=30,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("10.00"),
        status="ready",
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


@pytest.fixture
def digest_env():
    """Point the sender at the test database, with a delivering mail backend."""
    settings = digest_sender.get_settings().model_copy(update={
        "content_digest_enabled": True,
        "content_digest_hour_utc": 0,  # always past due, so a run is always claimable
        "email_backend": "resend",
        "resend_api_key": "test-key",
    })
    with patch.object(digest_sender, "AsyncSessionLocal", TestSessionLocal), \
            patch.object(digest_sender, "get_settings", return_value=settings):
        yield settings


@pytest.fixture
def sent_ok():
    with patch.object(
        email_service, "send_bulk_email", new=AsyncMock(return_value=(1, 0))
    ) as send:
        yield send


@pytest.mark.asyncio
async def test_a_digest_goes_out_and_is_recorded(db_session, digest_env, sent_ok):
    producer = await _user(db_session, role=UserRole.producer)
    loop = await _ready_loop(db_session, producer.id)

    run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.sent
    assert (run.items, run.sent, run.failed) == (1, 1, 0)
    assert run.trigger == "beat"
    assert str(loop.id) in run.claimed_ids["loop"]
    sent_ok.assert_awaited_once()

    await db_session.refresh(loop)
    assert loop.announced_at is not None


@pytest.mark.asyncio
async def test_an_unconfigured_backend_claims_nothing(db_session, digest_env):
    """The failure that used to look like success.

    send_email skips silently without credentials and send_bulk_email counts a
    skip as sent, so the run reported sent=N while the list received nothing —
    and the content was stamped, so it never went out later either.
    """
    producer = await _user(db_session, role=UserRole.producer)
    loop = await _ready_loop(db_session, producer.id)
    digest_env.resend_api_key = ""

    with patch.object(email_service, "send_bulk_email", new=AsyncMock()) as send:
        run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.not_configured
    assert "RESEND_API_KEY" in run.detail
    send.assert_not_awaited()

    await db_session.refresh(loop)
    assert loop.announced_at is None, "content must survive an unsendable run"


@pytest.mark.asyncio
async def test_a_send_that_fails_entirely_releases_the_content(db_session, digest_env):
    producer = await _user(db_session, role=UserRole.producer)
    loop = await _ready_loop(db_session, producer.id)

    with patch.object(
        email_service, "send_bulk_email", new=AsyncMock(return_value=(0, 1))
    ):
        run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.failed
    assert run.claimed_ids["loop"] == [str(loop.id)]

    await db_session.refresh(loop)
    assert loop.announced_at is None, "nothing was delivered, so nothing was announced"


@pytest.mark.asyncio
async def test_a_partial_send_keeps_the_claim(db_session, digest_env):
    """Some addresses got it, so re-sending the lot tomorrow is the worse error."""
    producer = await _user(db_session, role=UserRole.producer)
    loop = await _ready_loop(db_session, producer.id)

    with patch.object(
        email_service, "send_bulk_email", new=AsyncMock(return_value=(1, 3))
    ):
        run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.sent
    await db_session.refresh(loop)
    assert loop.announced_at is not None


@pytest.mark.asyncio
async def test_a_raising_send_is_recorded_rather_than_lost(db_session, digest_env):
    producer = await _user(db_session, role=UserRole.producer)
    await _ready_loop(db_session, producer.id)

    with patch.object(
        email_service, "send_bulk_email", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.failed
    assert "RuntimeError" in run.detail


@pytest.mark.asyncio
async def test_only_one_scheduler_can_send_a_given_slot(db_session, digest_env, sent_ok):
    """Beat and the API's own scheduler both fire; the list gets one email."""
    producer = await _user(db_session, role=UserRole.producer)
    await _ready_loop(db_session, producer.id)

    first = await digest_sender.run_digest(trigger="beat")
    second = await digest_sender.run_digest(trigger="api")

    assert first.status == DigestRunStatus.sent
    assert second is None
    assert sent_ok.await_count == 1


@pytest.mark.asyncio
async def test_a_manual_run_does_not_consume_the_scheduled_slot(
    db_session, digest_env, sent_ok
):
    producer = await _user(db_session, role=UserRole.producer)
    await _ready_loop(db_session, producer.id)

    manual = await digest_sender.run_digest(trigger="manual", force=True)
    await _ready_loop(db_session, producer.id, title="Later Loop")
    scheduled = await digest_sender.run_digest(trigger="beat")

    assert manual.run_key.startswith("manual:")
    assert scheduled is not None and scheduled.status == DigestRunStatus.sent
    assert sent_ok.await_count == 2


@pytest.mark.asyncio
async def test_nothing_new_still_leaves_a_run(db_session, digest_env, sent_ok):
    await _user(db_session)

    run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.empty
    sent_ok.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_recipients_claims_nothing(db_session, digest_env, sent_ok):
    producer = await _user(db_session, role=UserRole.producer)
    loop = await _ready_loop(db_session, producer.id)

    with patch(
        "app.services.broadcast_service.resolve_recipients",
        new=AsyncMock(return_value=[]),
    ):
        run = await digest_sender.run_digest(trigger="beat")

    assert run.status == DigestRunStatus.no_recipients
    sent_ok.assert_not_awaited()
    await db_session.refresh(loop)
    assert loop.announced_at is None


@pytest.mark.asyncio
async def test_a_disabled_digest_does_nothing_at_all(db_session, digest_env, sent_ok):
    producer = await _user(db_session, role=UserRole.producer)
    await _ready_loop(db_session, producer.id)
    digest_env.content_digest_enabled = False

    assert await digest_sender.run_digest(trigger="beat") is None
    sent_ok.assert_not_awaited()
