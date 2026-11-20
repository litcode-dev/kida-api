"""The sweep that removes email/password sign-ups that never verified."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.ai_generation import AIGeneration, AIProvider
from app.models.newsletter import NewsletterSubscriber
from app.models.user import User, UserRole
from app.services import auth_service

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(hours=72)
OLD = NOW - timedelta(days=4)
RECENT = NOW - timedelta(hours=1)


def _user(email, *, created_at=OLD, verified=False, provider=None,
          password_hash="x", role=UserRole.user):
    return User(
        email=email,
        full_name="Someone",
        password_hash=password_hash,
        role=role,
        is_verified=verified,
        oauth_provider=provider,
        created_at=created_at,
    )


async def _emails(db):
    return set((await db.scalars(select(User.email))).all())


@pytest.mark.asyncio
async def test_old_unverified_password_account_is_deleted(db_session):
    db_session.add(_user("stale@test.com"))
    await db_session.commit()

    result = await auth_service.purge_unverified_accounts(db_session, TTL, now=NOW)

    assert result == {"deleted": 1, "skipped": 0}
    assert await _emails(db_session) == set()


@pytest.mark.asyncio
async def test_accounts_that_do_not_qualify_are_kept(db_session):
    db_session.add_all([
        _user("recent@test.com", created_at=RECENT),
        _user("verified@test.com", verified=True),
        _user("google@test.com", provider="google"),
        _user("oauthonly@test.com", password_hash=None),
        _user("producer@test.com", role=UserRole.producer),
        _user("admin@test.com", role=UserRole.admin),
    ])
    await db_session.commit()

    result = await auth_service.purge_unverified_accounts(db_session, TTL, now=NOW)

    assert result == {"deleted": 0, "skipped": 0}
    assert len(await _emails(db_session)) == 6


@pytest.mark.asyncio
async def test_referenced_account_is_skipped_not_cascaded(db_session):
    referenced = _user("referenced@test.com")
    stale = _user("stale@test.com")
    db_session.add_all([referenced, stale])
    await db_session.commit()
    db_session.add(AIGeneration(user_id=referenced.id, provider=AIProvider.suno, prompt="p"))
    await db_session.commit()

    result = await auth_service.purge_unverified_accounts(db_session, TTL, now=NOW)

    assert result == {"deleted": 1, "skipped": 1}
    assert await _emails(db_session) == {"referenced@test.com"}
    assert len((await db_session.scalars(select(AIGeneration))).all()) == 1


@pytest.mark.asyncio
async def test_newsletter_signup_for_the_address_survives(db_session):
    # Nobody proved they own the address, so a newsletter signup for it may be
    # the real owner's and must not go with the unverified account.
    db_session.add(_user("shared@test.com"))
    db_session.add(NewsletterSubscriber(email="shared@test.com"))
    await db_session.commit()

    await auth_service.purge_unverified_accounts(db_session, TTL, now=NOW)

    subs = (await db_session.scalars(select(NewsletterSubscriber.email))).all()
    assert subs == ["shared@test.com"]


@pytest.mark.asyncio
async def test_address_can_register_again_after_purge(client, db_session):
    payload = {"email": "again@test.com", "password": "pass1234", "full_name": "Again"}
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 200

    await auth_service.purge_unverified_accounts(
        db_session, TTL, now=datetime.now(timezone.utc) + timedelta(days=4)
    )

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 200
