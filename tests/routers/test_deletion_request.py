"""The deletion route for people who cannot sign in.

The public page offers this to anyone who has lost their password, their Google
account, or their phone. Two things make it safe to expose: possession of the
inbox is the only way through it, and it answers identically whether or not the
address is registered.
"""
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.user import User, UserRole
from app.services import deletion_request_service
from app.services.auth_service import hash_password


def _no_side_effects():
    """The confirm route now runs a real deletion; keep it off the network."""
    return [
        patch("app.services.email_service.send_email", new=AsyncMock()),
        patch("app.services.s3_service.delete_object", new=AsyncMock()),
        patch(
            "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
            new=lambda *a, **kw: None,
        ),
        patch(
            "app.tasks.deletion_tasks.propagate_account_deletion.delay",
            new=lambda *a, **kw: None,
        ),
    ]


async def _confirm(client, token):
    patchers = _no_side_effects()
    for p in patchers:
        p.start()
    try:
        return await client.post(
            "/api/v1/auth/deletion-request/confirm", json={"token": token}
        )
    finally:
        for p in patchers:
            p.stop()

EMAIL = "leaver@test.com"
PASSWORD = "pass1234"


async def _make_user(db, email=EMAIL):
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=await hash_password(PASSWORD),
        full_name="Leaver",
        role=UserRole.user,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── The token ────────────────────────────────────────────────────────────────

def test_a_token_round_trips_to_the_address_it_was_made_for():
    token = deletion_request_service.make_token("Leaver@Test.com ")
    assert deletion_request_service.parse_token(token) == EMAIL


def test_a_tampered_address_invalidates_the_token():
    """The address is signed, so it cannot be swapped for someone else's."""
    token = deletion_request_service.make_token(EMAIL)
    version, expiry, _, signature = token.split(".")
    import base64
    forged_email = base64.urlsafe_b64encode(b"victim@test.com").decode().rstrip("=")
    forged = ".".join([version, expiry, forged_email, signature])

    assert deletion_request_service.parse_token(forged) is None


def test_a_tampered_expiry_invalidates_the_token():
    """The expiry is inside the signed material, not alongside it."""
    token = deletion_request_service.make_token(EMAIL)
    version, expiry, email, signature = token.split(".")
    extended = ".".join([version, str(int(expiry) + 86400), email, signature])

    assert deletion_request_service.parse_token(extended) is None


def test_an_expired_token_is_refused():
    token = deletion_request_service.make_token(EMAIL, now=int(time.time()) - 90_000)
    assert deletion_request_service.parse_token(token) is None


@pytest.mark.parametrize(
    "token", ["", "garbage", "v1.notanumber.abc.def", "v2.1.2.3", "a.b.c.d.e"]
)
def test_malformed_tokens_are_refused_without_raising(token):
    assert deletion_request_service.parse_token(token) is None


def test_the_token_is_not_an_unsubscribe_token():
    """Purpose-separated signatures: one signing key, no cross-protocol replay."""
    from app.services import unsubscribe_service

    assert deletion_request_service.parse_token(unsubscribe_service.make_token(EMAIL)) is None


@pytest.mark.asyncio
async def test_a_token_can_only_be_claimed_once(fake_redis):
    token = deletion_request_service.make_token(EMAIL)
    assert await deletion_request_service.claim_token(fake_redis, token) is True
    assert await deletion_request_service.claim_token(fake_redis, token) is False


# ── Requesting ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_requesting_deletion_emails_a_link_to_a_registered_address(
    client, db_session
):
    user = await _make_user(db_session)
    with patch(
        "app.tasks.deletion_tasks.send_deletion_request_email.delay"
    ) as queued:
        resp = await client.post(
            "/api/v1/auth/deletion-request", json={"email": EMAIL}
        )

    assert resp.status_code == 200
    queued.assert_called_once_with(str(user.id))


@pytest.mark.asyncio
async def test_requesting_deletion_matches_the_address_case_insensitively(
    client, db_session
):
    user = await _make_user(db_session)
    with patch(
        "app.tasks.deletion_tasks.send_deletion_request_email.delay"
    ) as queued:
        await client.post(
            "/api/v1/auth/deletion-request", json={"email": "LEAVER@TEST.COM"}
        )
    queued.assert_called_once_with(str(user.id))


@pytest.mark.asyncio
async def test_an_unregistered_address_is_answered_identically(client, db_session):
    """This endpoint must not become a way to test who has an account."""
    await _make_user(db_session)

    with patch("app.tasks.deletion_tasks.send_deletion_request_email.delay"):
        registered = await client.post(
            "/api/v1/auth/deletion-request", json={"email": EMAIL}
        )
    unregistered = await client.post(
        "/api/v1/auth/deletion-request", json={"email": "nobody@test.com"}
    )

    assert registered.status_code == unregistered.status_code == 200
    assert registered.json() == unregistered.json()


@pytest.mark.asyncio
async def test_requesting_deletion_does_not_delete_anything_by_itself(
    client, db_session
):
    """Control of the inbox has to be shown first — the link is the only trigger."""
    user = await _make_user(db_session)
    with patch("app.tasks.deletion_tasks.send_deletion_request_email.delay"):
        await client.post("/api/v1/auth/deletion-request", json={"email": EMAIL})

    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == user.id)
    ) == 1


@pytest.mark.asyncio
async def test_the_request_endpoint_is_rate_limited(client, db_session):
    """It is public, unauthenticated, and sends mail — so it has to have a ceiling.

    The autouse fixture disables rate limiting for every other test; this one
    turns it back on for the duration.
    """
    from app.middleware.rate_limit import limiter

    await _make_user(db_session)
    limiter.enabled = True
    limiter.reset()
    try:
        with patch("app.tasks.deletion_tasks.send_deletion_request_email.delay"):
            codes = [
                (
                    await client.post(
                        "/api/v1/auth/deletion-request", json={"email": EMAIL}
                    )
                ).status_code
                for _ in range(5)
            ]
    finally:
        limiter.enabled = False
        limiter.reset()

    assert 429 in codes, f"expected a rate limit, got {codes}"


# ── Confirming ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirming_deletes_the_account_outright(client, db_session):
    user = await _make_user(db_session)

    resp = await _confirm(client, deletion_request_service.make_token(EMAIL))

    assert resp.status_code == 200
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == user.id)
    ) == 0


@pytest.mark.asyncio
async def test_a_confirmed_deletion_ends_the_session_immediately(client, db_session):
    user = await _make_user(db_session)
    from app.services.auth_service import create_access_token

    headers = {"Authorization": f"Bearer {create_access_token(str(user.id), 'user')}"}

    await _confirm(client, deletion_request_service.make_token(EMAIL))

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_the_password_stops_working_after_a_confirmed_deletion(
    client, db_session
):
    """There is no window to sign back in through — the account is simply gone."""
    await _make_user(db_session)

    await _confirm(client, deletion_request_service.make_token(EMAIL))
    login = await client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )

    assert login.status_code == 401


@pytest.mark.asyncio
async def test_a_replayed_link_cannot_touch_a_new_account_on_the_same_address(
    client, db_session
):
    """Delete, sign up again with the same address — the spent link must not
    take the replacement account with it."""
    await _make_user(db_session)
    token = deletion_request_service.make_token(EMAIL)

    await _confirm(client, token)
    reg = await client.post("/api/v1/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Someone New",
    })
    new_id = uuid.UUID(reg.json()["data"]["id"])

    replay = await _confirm(client, token)

    assert replay.status_code == 200  # says nothing either way
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == new_id)
    ) == 1  # but did nothing


@pytest.mark.asyncio
async def test_an_invalid_token_is_answered_like_a_valid_one(client, db_session):
    await _make_user(db_session)

    good = await _confirm(client, deletion_request_service.make_token(EMAIL))
    bad = await _confirm(client, "forged.1.2.3")

    assert good.status_code == bad.status_code == 200
    assert good.json()["message"] == bad.json()["message"]


@pytest.mark.asyncio
async def test_a_token_for_an_address_with_no_account_is_a_quiet_no_op(
    client, db_session
):
    resp = await _confirm(client, deletion_request_service.make_token("ghost@test.com"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_the_audit_says_the_deletion_was_asked_for_by_email(client, db_session):
    """Which is the whole reason the route passes an actor through."""
    from app.models.deletion_audit import AccountDeletionAudit, DeletionActor

    await _make_user(db_session)

    await _confirm(client, deletion_request_service.make_token(EMAIL))

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.actor is DeletionActor.email_request


@pytest.mark.asyncio
async def test_the_confirmation_link_points_at_the_frontend_not_the_api():
    """A GET on an API endpoint would be actioned by link-prefetching mail
    clients; the frontend page asks for a real click instead."""
    url = deletion_request_service.confirmation_url("some-token")
    assert "/delete-account/confirm?token=some-token" in url
    assert "/api/v1/" not in url
