"""One click, no login, no confirmation step — and only for your own address."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.newsletter import NewsletterSubscriber
from app.models.user import User, UserRole
from app.services import unsubscribe_service
from app.services.auth_service import hash_password


def _url(email: str, token: str | None = None) -> str:
    token = unsubscribe_service.make_token(email) if token is None else token
    return f"/api/v1/newsletter/unsubscribe/one-click?email={email}&token={token}"


async def _subscriber(db, email: str, active: bool = True):
    db.add(NewsletterSubscriber(id=uuid.uuid4(), email=email, is_active=active))
    await db.commit()


def test_token_is_stable_and_address_specific():
    assert unsubscribe_service.make_token("a@test.com") == unsubscribe_service.make_token("a@test.com")
    assert unsubscribe_service.make_token("a@test.com") != unsubscribe_service.make_token("b@test.com")
    # Case and surrounding space must not change the signature, or a link
    # rendered from a differently-cased address would not verify.
    assert unsubscribe_service.make_token("A@Test.com ") == unsubscribe_service.make_token("a@test.com")


def test_token_verification_rejects_a_forgery():
    assert unsubscribe_service.verify_token("a@test.com", unsubscribe_service.make_token("a@test.com"))
    assert not unsubscribe_service.verify_token("a@test.com", "deadbeef")
    assert not unsubscribe_service.verify_token("a@test.com", "")
    # A token minted for someone else must not unsubscribe this address.
    assert not unsubscribe_service.verify_token(
        "a@test.com", unsubscribe_service.make_token("b@test.com")
    )


@pytest.mark.asyncio
async def test_one_click_get_unsubscribes(client, db_session):
    email = "reader@test.com"
    await _subscriber(db_session, email)

    resp = await client.get(_url(email))

    assert resp.status_code == 200
    assert "off the list" in resp.text
    row = await db_session.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == email)
    )
    await db_session.refresh(row)
    assert row.is_active is False
    assert row.unsubscribed_at is not None


@pytest.mark.asyncio
async def test_one_click_post_is_accepted(client, db_session):
    """RFC 8058: the mail client POSTs to the List-Unsubscribe URL itself."""
    email = "poster@test.com"
    await _subscriber(db_session, email)

    resp = await client.post(_url(email))

    assert resp.status_code == 200
    row = await db_session.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == email)
    )
    await db_session.refresh(row)
    assert row.is_active is False


@pytest.mark.asyncio
async def test_a_bad_token_changes_nothing(client, db_session):
    email = "safe@test.com"
    await _subscriber(db_session, email)

    resp = await client.get(_url(email, token="not-the-right-token"))

    assert resp.status_code == 400
    row = await db_session.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == email)
    )
    await db_session.refresh(row)
    assert row.is_active is True


@pytest.mark.asyncio
async def test_a_user_with_no_subscriber_row_is_still_suppressed(client, db_session):
    """The gap that made unsubscribe a no-op for most recipients.

    Registered users receive marketing because they have an account, not
    because they subscribed, so most have no subscriber row at all. The
    suppression list is that same table, so opting out has to create one.
    """
    user = User(
        id=uuid.uuid4(),
        email="account@test.com",
        password_hash=await hash_password("pass"),
        full_name="Account Holder",
        role=UserRole.user,
    )
    db_session.add(user)
    await db_session.commit()

    resp = await client.get(_url(user.email))

    assert resp.status_code == 200
    row = await db_session.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == user.email)
    )
    assert row is not None
    assert row.is_active is False


@pytest.mark.asyncio
async def test_clicking_twice_is_not_an_error(client, db_session):
    email = "twice@test.com"
    await _subscriber(db_session, email)

    first = await client.get(_url(email))
    second = await client.get(_url(email))

    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_the_json_endpoint_also_suppresses_an_unknown_address(client, db_session):
    with patch("app.routers.newsletter.send_email", new=AsyncMock()):
        resp = await client.post(
            "/api/v1/newsletter/unsubscribe", json={"email": "stranger@test.com"}
        )

    assert resp.status_code == 200
    row = await db_session.scalar(
        select(NewsletterSubscriber).where(
            NewsletterSubscriber.email == "stranger@test.com"
        )
    )
    assert row is not None and row.is_active is False
