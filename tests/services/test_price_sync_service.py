import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.price_sync import PriceSyncState
from app.services import price_sync_service


class FakeItem:
    """Minimal stand-in for a sellable ORM item (loop / drum kit / drone)."""

    def __init__(self, *, is_free=False, store_product_id=None, desired_price_usd="1.99",
                 state=PriceSyncState.pending.value):
        self.id = uuid.uuid4()
        self.title = "Test Item"
        self.description = "A test item"
        self.is_free = is_free
        self.store_product_id = store_product_id
        self.desired_price_usd = Decimal(desired_price_usd) if desired_price_usd else None
        self.price_sync_state = state
        self.price_sync_error = None
        self.price_synced_at = None


def test_ensure_sku_assigns_once_and_is_stable():
    item = FakeItem(store_product_id=None)
    sku = price_sync_service.ensure_sku(item, "loop")
    assert sku.startswith("kida.loop.")
    assert item.store_product_id == sku
    # calling again must not regenerate/reuse — SKUs are immutable
    assert price_sync_service.ensure_sku(item, "loop") == sku


def test_mark_price_dirty_resets_to_pending():
    item = FakeItem(state=PriceSyncState.synced.value)
    item.price_sync_error = "old error"
    price_sync_service.mark_price_dirty(item)
    assert item.price_sync_state == PriceSyncState.pending.value
    assert item.price_sync_error is None
    assert item.price_synced_at is None


@pytest.mark.asyncio
async def test_sync_item_new_apple_iap_goes_pending_review():
    item = FakeItem(store_product_id=None)
    with patch.object(price_sync_service.google_play_service, "upsert_product", AsyncMock()) as play, \
         patch.object(price_sync_service.app_store_service, "find_iap", AsyncMock(return_value=None)), \
         patch.object(price_sync_service.app_store_service, "create_iap", AsyncMock(return_value="iap-1")), \
         patch.object(price_sync_service.app_store_service, "ensure_localization", AsyncMock()), \
         patch.object(price_sync_service.app_store_service, "attach_review_screenshot", AsyncMock()), \
         patch.object(price_sync_service.app_store_service, "apply_nearest_price", AsyncMock(return_value=Decimal("1.99"))), \
         patch.object(price_sync_service.app_store_service, "submit_for_review", AsyncMock()) as submit:
        await price_sync_service.sync_item(item, "loop")

    play.assert_awaited_once()
    submit.assert_awaited_once()  # new IAP submitted for review
    assert item.price_sync_state == PriceSyncState.apple_pending_review.value
    assert item.store_product_id.startswith("kida.loop.")


@pytest.mark.asyncio
async def test_sync_item_existing_approved_iap_goes_synced():
    item = FakeItem(store_product_id="kida.kit.test_abcd1234")
    approved_iap = {"id": "iap-9", "attributes": {"state": "APPROVED"}}
    with patch.object(price_sync_service.google_play_service, "upsert_product", AsyncMock()), \
         patch.object(price_sync_service.app_store_service, "find_iap", AsyncMock(return_value=approved_iap)), \
         patch.object(price_sync_service.app_store_service, "apply_nearest_price", AsyncMock(return_value=Decimal("1.99"))), \
         patch.object(price_sync_service.app_store_service, "create_iap", AsyncMock()) as create:
        await price_sync_service.sync_item(item, "kit")

    create.assert_not_called()  # existing IAP, no re-create
    assert item.price_sync_state == PriceSyncState.synced.value
    assert item.price_synced_at is not None


@pytest.mark.asyncio
async def test_poll_apple_review_flips_to_synced_when_approved():
    item = FakeItem(store_product_id="kida.drone.test_abcd1234",
                    state=PriceSyncState.apple_pending_review.value)
    approved_iap = {"id": "iap-3", "attributes": {"state": "APPROVED"}}
    with patch.object(price_sync_service.app_store_service, "find_iap", AsyncMock(return_value=approved_iap)):
        await price_sync_service.poll_apple_review(item)
    assert item.price_sync_state == PriceSyncState.synced.value
    assert item.price_synced_at is not None


@pytest.mark.asyncio
async def test_poll_apple_review_stays_pending_when_not_approved():
    item = FakeItem(store_product_id="kida.drone.test_abcd1234",
                    state=PriceSyncState.apple_pending_review.value)
    pending_iap = {"id": "iap-3", "attributes": {"state": "WAITING_FOR_REVIEW"}}
    with patch.object(price_sync_service.app_store_service, "find_iap", AsyncMock(return_value=pending_iap)):
        await price_sync_service.poll_apple_review(item)
    assert item.price_sync_state == PriceSyncState.apple_pending_review.value


@pytest.mark.asyncio
async def test_sync_pending_records_error_and_continues(db_session):
    from app.models.loop import Loop, Genre, TempoFeel
    from app.models.user import User, UserRole
    from app.services.auth_service import hash_password

    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=UserRole.admin,
    )
    db_session.add(user)
    await db_session.flush()

    loop = Loop(
        id=uuid.uuid4(),
        title="Paid Loop",
        slug=f"paid-loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=120,
        duration=8,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("2.99"),
        is_free=False,
        is_paid=True,
        desired_price_usd=Decimal("2.99"),
        store_product_id="kida.loop.paid_loop_abcd1234",
        price_sync_state=PriceSyncState.pending.value,
        created_by=user.id,
    )
    db_session.add(loop)
    await db_session.commit()

    # Play upsert blows up — the item must be marked error, run must not raise.
    with patch.object(price_sync_service.google_play_service, "upsert_product",
                      AsyncMock(side_effect=RuntimeError("play boom"))):
        counts = await price_sync_service.sync_pending(db_session)

    await db_session.refresh(loop)
    assert loop.price_sync_state == PriceSyncState.error.value
    assert "play boom" in loop.price_sync_error
    assert counts["errors"] == 1
