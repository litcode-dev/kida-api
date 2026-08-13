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


# ── the token must stay a signature, not a credential ─────────────────────────


def test_token_is_not_a_session_token():
    """It signs an address and carries nothing else.

    SECRET_KEY also signs access tokens, so the unsubscribe signature is
    domain-separated: it must not be decodable as a JWT, and must not embed the
    address in a recoverable form.
    """
    import base64

    token = unsubscribe_service.make_token("reader@test.com")

    assert token.isalnum() and len(token) == 32
    assert token.count(".") == 0, "a JWT-shaped token would suggest key confusion"
    for probe in ("reader", "test.com", "reader@test.com"):
        assert probe not in token
        assert base64.b64encode(probe.encode()).decode().rstrip("=") not in token


def test_purpose_separation_changes_the_signature():
    """A bare HMAC of the address, without the purpose string, must not verify —
    otherwise a signature made elsewhere with the same key would be accepted."""
    import hashlib
    import hmac

    from app.config import get_settings

    email = "reader@test.com"
    bare = hmac.new(
        get_settings().secret_key.encode(), email.encode(), hashlib.sha256
    ).hexdigest()[:32]

    assert not unsubscribe_service.verify_token(email, bare)


@pytest.mark.asyncio
async def test_the_address_is_escaped_into_the_page(client, db_session):
    """The address is reflected into HTML, so it is escaped even though a valid
    signature is needed to get here."""
    email = 'x<script>alert(1)</script>@test.com'
    await _subscriber(db_session, email)

    resp = await client.get(_url(email))

    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


@pytest.mark.asyncio
async def test_an_invalid_link_reveals_nothing_about_the_address(client, db_session):
    """A wrong token is rejected before any lookup, so the page cannot be used
    to probe whether an address is on the list."""
    await _subscriber(db_session, "known@test.com")

    known = await client.get(_url("known@test.com", token="wrong"))
    unknown = await client.get(_url("nobody@test.com", token="wrong"))

    assert known.status_code == unknown.status_code == 400
    assert known.text == unknown.text.replace("nobody@test.com", "known@test.com")
