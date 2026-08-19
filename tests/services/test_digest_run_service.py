"""Which run is due, and who gets to run it.

The slot arithmetic is shared by the beat task, the API scheduler and the
diagnostic script, so a disagreement here would show up as a digest that runs
twice or not at all. The claim is what keeps those three from sending the same
email to the same list.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.digest_run import DigestRunStatus
from app.services import digest_run_service, email_service


def _at(hour, minute=0, day=19):
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


# ── Slot arithmetic ──────────────────────────────────────────────────────────

def test_after_the_hour_the_due_slot_is_today():
    assert digest_run_service.due_slot(17, _at(18, 30)) == _at(17)


def test_before_the_hour_the_due_slot_is_yesterday():
    assert digest_run_service.due_slot(17, _at(3)) == _at(17, day=18)


def test_the_hour_itself_counts_as_due():
    assert digest_run_service.due_slot(17, _at(17)) == _at(17)


def test_next_slot_is_a_day_after_the_due_one():
    assert digest_run_service.next_slot(17, _at(18)) == _at(17, day=20)


def test_slot_keys_are_one_per_day():
    assert digest_run_service.slot_key(_at(17)) == "2026-08-19T17"
    assert digest_run_service.slot_key(_at(17, day=20)) == "2026-08-20T17"


def test_a_manual_key_never_collides_with_a_slot():
    key = digest_run_service.manual_key(_at(17))
    assert key.startswith("manual:")
    assert key != digest_run_service.slot_key(_at(17))


def test_slots_are_computed_in_utc_whatever_the_caller_passes():
    lagos = timezone(timedelta(hours=1))
    assert digest_run_service.due_slot(17, _at(18, 30).astimezone(lagos)) == _at(17)


# ── Claiming ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_claimant_wins_and_the_second_gets_nothing(db_session):
    key = digest_run_service.slot_key(_at(17))

    first = await digest_run_service.claim(
        db_session, key=key, scheduled_for=_at(17), trigger="beat"
    )
    second = await digest_run_service.claim(
        db_session, key=key, scheduled_for=_at(17), trigger="api"
    )

    assert first is not None and first.trigger == "beat"
    assert second is None, "a second process must not send the same digest"


@pytest.mark.asyncio
async def test_a_different_slot_is_claimable(db_session):
    await digest_run_service.claim(
        db_session, key=digest_run_service.slot_key(_at(17)),
        scheduled_for=_at(17), trigger="beat",
    )
    tomorrow = await digest_run_service.claim(
        db_session, key=digest_run_service.slot_key(_at(17, day=20)),
        scheduled_for=_at(17, day=20), trigger="beat",
    )

    assert tomorrow is not None


@pytest.mark.asyncio
async def test_finishing_records_the_outcome(db_session):
    run = await digest_run_service.claim(
        db_session, key="2026-08-19T17", scheduled_for=_at(17), trigger="beat"
    )

    await digest_run_service.finish(
        db_session, run, DigestRunStatus.sent,
        items=3, recipients=40, sent=40, claimed_ids={"loop": ["abc"]},
    )
    found = await digest_run_service.find_by_key(db_session, "2026-08-19T17")

    assert found.status == DigestRunStatus.sent
    assert (found.items, found.recipients, found.sent) == (3, 40, 40)
    assert found.finished_at is not None
    assert found.claimed_ids == {"loop": ["abc"]}


@pytest.mark.asyncio
async def test_recent_runs_are_newest_first(db_session):
    for day in (17, 18, 19):
        run = await digest_run_service.claim(
            db_session, key=f"2026-08-{day}T17",
            scheduled_for=_at(17, day=day), trigger="beat",
        )
        run.started_at = _at(17, day=day)
        await digest_run_service.finish(db_session, run, DigestRunStatus.empty)

    keys = [r.run_key for r in await digest_run_service.recent(db_session, limit=2)]

    assert keys == ["2026-08-19T17", "2026-08-18T17"]


# ── The credentials check the digest refuses to run without ──────────────────

class _Settings:
    def __init__(self, backend, resend="", user="", password=""):
        self.email_backend = backend
        self.resend_api_key = resend
        self.smtp_user = user
        self.smtp_password = password


def test_resend_without_a_key_would_drop_everything():
    problem = email_service.delivery_problem(_Settings("resend"))
    assert problem and "RESEND_API_KEY" in problem


def test_smtp_without_credentials_would_drop_everything():
    problem = email_service.delivery_problem(_Settings("smtp"))
    assert problem and "SMTP_USER" in problem


def test_fallback_needs_only_one_of_the_two():
    assert email_service.delivery_problem(_Settings("fallback", resend="key")) is None
    assert email_service.delivery_problem(
        _Settings("fallback", user="u", password="p")
    ) is None
    assert email_service.delivery_problem(_Settings("fallback")) is not None


def test_a_configured_backend_reports_no_problem():
    assert email_service.delivery_problem(_Settings("resend", resend="key")) is None
