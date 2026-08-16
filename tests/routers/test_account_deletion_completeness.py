"""What the public account-deletion page promises, held to by tests.

The page tells people three things the server has to actually do: every session
ends, the data is gone from the third parties we sent it to, and asking twice is
not an error. Each of those is a test here.
"""
import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.deletion_audit import (
    AccountDeletionAudit,
    AccountDeletionPropagation,
    DeletionActor,
    PropagationStatus,
)
from app.models.iap_subscription import IapPlatform, IapSubscription, IapSubscriptionStatus
from app.models.user import User, UserRole
from app.services import account_deletion_service, auth_service
from app.services.auth_service import (
    REFRESH_INDEX_PREFIX,
    REFRESH_PREFIX,
    create_access_token,
    hash_password,
)

PASSWORD = "pass1234"
EMAIL = "leaver@test.com"


async def _make_user(db, email=EMAIL, player_id="player-abc"):
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=await hash_password(PASSWORD),
        full_name="Leaver",
        role=UserRole.user,
        is_verified=True,
        onesignal_player_id=player_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _delete_account(client, user, refresh_token=None):
    body = {"refresh_token": refresh_token} if refresh_token else {}
    return await client.request("DELETE", "/api/v1/auth/me", headers=_headers(user), json=body)


async def _purge(db, user, redis, refresh_token=None, actor="user", **patches):
    """Run the real deletion with the outbound side effects stubbed."""
    defaults = {
        "app.services.s3_service.delete_object": AsyncMock(),
        "app.services.email_service.send_email": AsyncMock(),
        "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay":
            lambda *a, **kw: None,
        "app.tasks.deletion_tasks.propagate_account_deletion.delay": lambda *a, **kw: None,
    }
    defaults.update(patches)
    patchers = [patch(target, new=new) for target, new in defaults.items()]
    for p in patchers:
        p.start()
    try:
        await auth_service.delete_user(db, user, redis, refresh_token, actor=actor)
    finally:
        for p in patchers:
            p.stop()


# ── Every session ends, not just the one that asked ──────────────────────────

@pytest.mark.asyncio
async def test_deleting_revokes_refresh_tokens_from_other_devices(
    client, db_session, fake_redis
):
    """The phone deletes the account; the tablet's token must not survive it."""
    user = await _make_user(db_session)
    phone, tablet = "tok-phone", "tok-tablet"
    await auth_service.store_refresh_token(fake_redis, phone, str(user.id))
    await auth_service.store_refresh_token(fake_redis, tablet, str(user.id))

    await _purge(db_session, user, fake_redis, refresh_token=phone)

    assert await fake_redis.get(f"{REFRESH_PREFIX}{phone}") is None
    assert await fake_redis.get(f"{REFRESH_PREFIX}{tablet}") is None
    assert await fake_redis.smembers(f"{REFRESH_INDEX_PREFIX}{user.id}") == set()


@pytest.mark.asyncio
async def test_revoking_one_session_leaves_the_others_signed_in(db_session, fake_redis):
    """The index must not turn a single logout into a global one."""
    user = await _make_user(db_session)
    await auth_service.store_refresh_token(fake_redis, "tok-a", str(user.id))
    await auth_service.store_refresh_token(fake_redis, "tok-b", str(user.id))

    await auth_service.revoke_refresh_token(fake_redis, "tok-a")

    assert await fake_redis.get(f"{REFRESH_PREFIX}{'tok-b'}") == str(user.id)
    assert await fake_redis.smembers(f"{REFRESH_INDEX_PREFIX}{user.id}") == {"tok-b"}


@pytest.mark.asyncio
async def test_one_users_deletion_does_not_touch_another_users_sessions(
    db_session, fake_redis
):
    leaver = await _make_user(db_session)
    stayer = await _make_user(db_session, email="stayer@test.com")
    await auth_service.store_refresh_token(fake_redis, "tok-leaver", str(leaver.id))
    await auth_service.store_refresh_token(fake_redis, "tok-stayer", str(stayer.id))

    await auth_service.revoke_all_refresh_tokens(fake_redis, str(leaver.id))

    assert await fake_redis.get(f"{REFRESH_PREFIX}tok-stayer") == str(stayer.id)


@pytest.mark.asyncio
async def test_revoke_all_survives_a_redis_outage(db_session, fake_redis):
    """A broken Redis must not stop an account being deleted."""
    user = await _make_user(db_session)

    async def _boom(*a, **kw):
        raise ConnectionError("redis is down")

    with patch.object(fake_redis, "smembers", new=_boom):
        assert await auth_service.revoke_all_refresh_tokens(fake_redis, str(user.id)) == 0


# ── Asking twice ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deleting_twice_does_not_email_twice(client, db_session):
    """The second call has no account to authenticate, so it never gets that far."""
    user = await _make_user(db_session)
    with patch("app.services.email_service.send_email", new=AsyncMock()) as send, \
         patch("app.services.s3_service.delete_object", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ), \
         patch(
             "app.tasks.deletion_tasks.propagate_account_deletion.delay",
             new=lambda *a, **kw: None,
         ):
        first = await _delete_account(client, user)
        second = await _delete_account(client, user)

    assert (first.status_code, second.status_code) == (200, 401)
    assert send.await_count == 1


# ── The audit trail ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_writes_an_audit_row(db_session, fake_redis):
    user = await _make_user(db_session)
    uid = user.id
    await _purge(db_session, user, fake_redis)

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.subject_hash == account_deletion_service.subject_hash(uid)
    assert audit.actor is DeletionActor.user
    assert audit.purged_at is not None


@pytest.mark.asyncio
async def test_the_audit_row_holds_no_personal_data(db_session, fake_redis):
    """The trail proves a deletion happened without being a list of who used the app."""
    user = await _make_user(db_session, email="private@test.com")
    await _purge(db_session, user, fake_redis)

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    contents = " ".join(
        str(getattr(audit, c.name)) for c in AccountDeletionAudit.__table__.columns
    )
    assert "private@test.com" not in contents
    assert "Leaver" not in contents
    assert str(user.id) not in contents


@pytest.mark.asyncio
async def test_the_subject_hash_is_recomputable_from_a_user_id(db_session, fake_redis):
    """Which is what answers "here is my id, prove you deleted me"."""
    user = await _make_user(db_session)
    uid = user.id
    await _purge(db_session, user, fake_redis)

    found = (
        await db_session.scalars(
            select(AccountDeletionAudit).where(
                AccountDeletionAudit.subject_hash
                == account_deletion_service.subject_hash(uid)
            )
        )
    ).one()
    assert found is not None


@pytest.mark.asyncio
async def test_the_audit_records_a_deletion_asked_for_by_email(db_session, fake_redis):
    user = await _make_user(db_session)

    await _purge(db_session, user, fake_redis, actor="email_request")

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.actor is DeletionActor.email_request
    # The account holder asked, and it was carried out there and then.
    assert audit.requested_at is not None


@pytest.mark.asyncio
async def test_an_admin_removal_records_no_request_time(db_session, fake_redis):
    """Nobody asked — requested_at is what separates that from a self-deletion."""
    user = await _make_user(db_session)

    await _purge(db_session, user, fake_redis, actor="admin")

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.actor is DeletionActor.admin
    assert audit.requested_at is None


# ── Third-party propagation ──────────────────────────────────────────────────

async def _purge_and_get_propagation(db, user, redis):
    await _purge(db, user, redis)
    return (await db.scalars(select(AccountDeletionPropagation))).one()


# These modules do `from app.config import get_settings`, so the name has to be
# replaced in each of them — patching app.config alone leaves the real settings
# bound and the propagation code then calls the live APIs.
_SETTINGS_CONSUMERS = (
    "app.services.account_deletion_service",
    "app.services.onesignal_service",
    "app.services.revenuecat_service",
)


@contextlib.contextmanager
def _configured(
    revenuecat_api_key="sk_test",
    onesignal_api_key="key",
    onesignal_app_id="00000000-0000-0000-0000-000000000000",
    max_attempts=8,
):
    fake = SimpleNamespace(
        revenuecat_api_key=revenuecat_api_key,
        revenuecat_base_url="https://api.revenuecat.test/v1",
        onesignal_api_key=onesignal_api_key,
        onesignal_app_id=onesignal_app_id,
        deletion_propagation_max_attempts=max_attempts,
    )
    with contextlib.ExitStack() as stack:
        for module in _SETTINGS_CONSUMERS:
            stack.enter_context(patch(f"{module}.get_settings", lambda: fake))
        yield fake


@contextlib.contextmanager
def _third_parties(rc=True, os_user=True, os_sub=True):
    """Stub both third-party clients. Values are what the delete helpers return."""
    with patch(
        "app.services.revenuecat_service.RevenueCatClient.delete_subscriber",
        new=AsyncMock(return_value=rc),
    ), patch(
        "app.services.onesignal_service.delete_user_by_external_id",
        new=AsyncMock(return_value=os_user),
    ), patch(
        "app.services.onesignal_service.delete_subscription",
        new=AsyncMock(return_value=os_sub),
    ):
        yield


@pytest.mark.asyncio
async def test_purge_queues_the_third_party_deletions(db_session, fake_redis):
    user = await _make_user(db_session)
    uid = user.id
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    assert row.onesignal_subscription_id == "player-abc"
    assert row.onesignal_external_id == str(uid)
    assert str(uid) in row.revenuecat_app_user_ids
    assert EMAIL in row.revenuecat_app_user_ids


@pytest.mark.asyncio
async def test_the_queue_carries_the_revenuecat_id_actually_on_file(
    db_session, fake_redis
):
    """RevenueCat may know the person by an id neither we nor the client guessed."""
    user = await _make_user(db_session)
    db_session.add(
        IapSubscription(
            user_id=user.id,
            product_id="kida_premium_monthly",
            platform=IapPlatform.android,
            status=IapSubscriptionStatus.active,
            store_transaction_id="txn-1",
            app_user_id="$RCAnonymousID:deadbeef",
        )
    )
    await db_session.commit()

    row = await _purge_and_get_propagation(db_session, user, fake_redis)
    assert "$RCAnonymousID:deadbeef" in row.revenuecat_app_user_ids


@pytest.mark.asyncio
async def test_propagation_deletes_the_subscriber_and_the_push_user(
    db_session, fake_redis
):
    user = await _make_user(db_session)
    uid = user.id
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    deleted_rc, deleted_os = [], []

    async def _rc(self, app_user_id):
        deleted_rc.append(app_user_id)
        return True

    async def _os_user(external_id):
        deleted_os.append(("user", external_id))
        return True

    async def _os_sub(subscription_id):
        deleted_os.append(("subscription", subscription_id))
        return True

    with _configured(), patch(
        "app.services.revenuecat_service.RevenueCatClient.delete_subscriber", new=_rc
    ), patch(
        "app.services.onesignal_service.delete_user_by_external_id", new=_os_user
    ), patch(
        "app.services.onesignal_service.delete_subscription", new=_os_sub
    ):
        finished = await account_deletion_service.propagate_one(db_session, row)

    assert finished is True
    assert str(uid) in deleted_rc and EMAIL in deleted_rc
    assert ("user", str(uid)) in deleted_os
    assert ("subscription", "player-abc") in deleted_os

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.revenuecat_status is PropagationStatus.done
    assert audit.onesignal_status is PropagationStatus.done


@pytest.mark.asyncio
async def test_a_finished_propagation_stops_holding_the_identifiers(
    db_session, fake_redis
):
    """The work queue is where the third-party ids live, and it empties itself."""
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    with _configured(), _third_parties():
        await account_deletion_service.propagate_one(db_session, row)

    remaining = await db_session.scalar(
        select(func.count()).select_from(AccountDeletionPropagation)
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_a_third_party_outage_does_not_undo_the_local_deletion(
    db_session, fake_redis
):
    """The whole point of best-effort: their downtime is not our data coming back."""
    user = await _make_user(db_session)
    uid = user.id
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    with _configured(), _third_parties(rc=False, os_user=False, os_sub=False):
        finished = await account_deletion_service.propagate_one(db_session, row)

    assert finished is False  # stays queued
    assert await db_session.get(User, uid) is None  # but the account is still gone


@pytest.mark.asyncio
async def test_a_failed_propagation_is_retried_with_a_backoff(db_session, fake_redis):
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)
    before = row.next_attempt_at

    with _configured(), _third_parties(rc=False, os_user=False, os_sub=False):
        await account_deletion_service.propagate_one(db_session, row)

    await db_session.refresh(row)
    assert row.attempts == 1
    assert row.next_attempt_at > before


@pytest.mark.asyncio
async def test_propagation_gives_up_and_says_so_rather_than_retrying_forever(
    db_session, fake_redis
):
    """A stuck deletion has to become visible, not loop quietly until the heat death."""
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)
    row.attempts = 7
    await db_session.commit()

    with _configured(), _third_parties(rc=False, os_user=False, os_sub=False):
        finished = await account_deletion_service.propagate_one(db_session, row)

    assert finished is True
    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.revenuecat_status is PropagationStatus.failed
    # And it stops holding the identifiers even though it never managed to send.
    assert await db_session.scalar(
        select(func.count()).select_from(AccountDeletionPropagation)
    ) == 0


@pytest.mark.asyncio
async def test_propagation_is_skipped_when_the_third_parties_are_not_configured(
    db_session, fake_redis
):
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    with _configured(revenuecat_api_key="", onesignal_api_key="", onesignal_app_id=""):
        finished = await account_deletion_service.propagate_one(db_session, row)

    assert finished is True
    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.revenuecat_status is PropagationStatus.skipped
    assert audit.onesignal_status is PropagationStatus.skipped


@pytest.mark.asyncio
async def test_the_sweep_only_picks_up_rows_whose_backoff_has_elapsed(
    db_session, fake_redis
):
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)
    row.next_attempt_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()

    assert (await account_deletion_service.run_pending(db_session))["considered"] == 0


@pytest.mark.asyncio
async def test_an_account_that_never_registered_a_device_is_still_deleted_upstream(
    db_session, fake_redis
):
    """No push registration on file does not mean nothing at OneSignal.

    The app may have called ``OneSignal.login(user.id)`` on a device that never
    reached ``/push/register-device``, so the external_id delete is still worth
    making — it 404s harmlessly when there is genuinely nothing there.
    """
    user = await _make_user(db_session, player_id=None)
    uid = user.id
    await _purge(db_session, user, fake_redis)

    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.onesignal_status is PropagationStatus.pending
    row = (await db_session.scalars(select(AccountDeletionPropagation))).one()
    assert row.onesignal_subscription_id is None
    assert row.onesignal_external_id == str(uid)


@pytest.mark.asyncio
async def test_a_404_from_a_third_party_counts_as_deleted(db_session, fake_redis):
    """Re-running a completed deletion must not look like a failure."""
    user = await _make_user(db_session)
    row = await _purge_and_get_propagation(db_session, user, fake_redis)

    import httpx

    async def _not_found(self, method, url, **kwargs):
        return httpx.Response(404, request=httpx.Request(method, url))

    with _configured(), patch.object(httpx.AsyncClient, "request", new=_not_found):
        finished = await account_deletion_service.propagate_one(db_session, row)

    assert finished is True
    audit = (await db_session.scalars(select(AccountDeletionAudit))).one()
    assert audit.revenuecat_status is PropagationStatus.done
    assert audit.onesignal_status is PropagationStatus.done
