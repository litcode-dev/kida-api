"""drop deleted_at from users

Account deletion no longer has a grace period. DELETE /auth/me and the confirmed
public deletion link both hard-delete straight away, so no row is ever left in a
pending state and the column has nothing left to hold.

Accounts still soft-deleted when this deploys are the awkward part. Dropping the
column out from under them would silently put them back into service — people
who asked to leave, restored by a schema change — so this finishes what they
asked for first, then drops the column.

Two things the SQL here cannot do that ``auth_service.delete_user`` does:

* **Third parties.** It cannot call RevenueCat or OneSignal. Instead it writes
  the same audit and propagation rows that function writes, so the existing beat
  sweep (``retry_pending_deletion_propagation``) picks the work up and finishes
  it minutes after the deploy, with retries and a backoff.
* **Stored files.** It has no S3 client. Any object keys belonging to these
  accounts are printed in the migration output rather than deleted, so an
  orphaned object is a line in the deploy log instead of a silent leak. In
  practice a soft-deleted listener has none; a producer with uploads will list
  them.

Everything else — the rows, in dependency order — is done here, mirroring
``delete_user``. Run ``python -m scripts.purge_pending_deletions`` before
deploying if you would rather these go through the real code path, S3 and all;
this migration then finds nothing to do.

Revision ID: q7v58q39r2s0
Revises: p6u47p28q1r9
Create Date: 2026-08-16 00:00:00.000000
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = "q7v58q39r2s0"
down_revision = "p6u47p28q1r9"
branch_labels = None
depends_on = None


# Rows keyed directly on the departing user.
_OWNED_BY_USER = (
    "ai_generations",
    "downloads",
    "likes",
    "purchases",
    "subscriptions",
    # No ON DELETE CASCADE, so anyone who ever held an entitlement would
    # otherwise fail the users delete on a foreign key.
    "iap_subscriptions",
)

# Rows keyed on the address rather than the id. The address is personal data
# too, and neither table is written case-consistently.
_OWNED_BY_EMAIL = ("newsletter_subscribers", "app_download_requests")


def _s3_keys(conn, uid) -> list[str]:
    """Stored objects belonging to this account, while its rows still exist."""
    keys: list[str] = []

    loops = conn.execute(sa.text(
        "SELECT id, file_s3_key, preview_s3_key, thumbnail_s3_key "
        "FROM loops WHERE created_by = :uid"
    ), {"uid": uid}).all()
    for loop_id, *stored in loops:
        keys += [k for k in stored if k]
        keys.append(f"loops/raw/{loop_id}.wav")

    stems = conn.execute(sa.text(
        "SELECT s.id, s.file_s3_key, s.preview_s3_key FROM stems s "
        "JOIN stem_packs p ON s.stem_pack_id = p.id WHERE p.created_by = :uid"
    ), {"uid": uid}).all()
    for stem_id, *stored in stems:
        keys += [k for k in stored if k]
        keys.append(f"stems/raw/{stem_id}.wav")

    keys += [k for row in conn.execute(sa.text(
        "SELECT t.file_s3_key, t.preview_s3_key FROM stem_arrangement_tracks t "
        "JOIN stem_arrangements a ON t.arrangement_id = a.id "
        "WHERE a.created_by = :uid OR a.stem_pack_id IN "
        "(SELECT id FROM stem_packs WHERE created_by = :uid)"
    ), {"uid": uid}).all() for k in row if k]

    keys += [k for (k,) in conn.execute(sa.text(
        "SELECT thumbnail_s3_key FROM drum_kits WHERE created_by = :uid"
    ), {"uid": uid}).all() if k]
    samples = conn.execute(sa.text(
        "SELECT s.id, s.file_s3_key, s.preview_s3_key FROM drum_samples s "
        "JOIN drum_kits k ON s.drum_kit_id = k.id WHERE k.created_by = :uid"
    ), {"uid": uid}).all()
    for sample_id, *stored in samples:
        keys += [k for k in stored if k]
        keys.append(f"drum-kits/raw/{sample_id}.wav")

    drone_ids = [r[0] for r in conn.execute(sa.text(
        "SELECT id FROM drones WHERE created_by = :uid OR category_id IN "
        "(SELECT id FROM drone_pad_categories WHERE created_by = :uid)"
    ), {"uid": uid}).all()]
    if drone_ids:
        keys += [k for row in conn.execute(sa.text(
            "SELECT file_s3_key, preview_s3_key, thumbnail_s3_key FROM drone_pads "
            "WHERE drone_id = ANY(:ids)"
        ), {"ids": drone_ids}).all() for k in row if k]
        keys += [f"drones/raw/{d}.wav" for d in drone_ids]

    return list(dict.fromkeys(k for k in keys if k))


def _queue_third_party_deletion(conn, uid, email, deleted_at) -> None:
    """Write the audit row and its propagation work, as ``delete_user`` does.

    The propagation row is what the beat sweep looks for, so RevenueCat and
    OneSignal still hear about these accounts — just a few minutes later than
    usual, and with the retries that path already has.
    """
    from app.services.account_deletion_service import subject_hash

    rc_ids = [r[0] for r in conn.execute(sa.text(
        "SELECT app_user_id FROM iap_subscriptions WHERE user_id = :uid"
    ), {"uid": uid}).all() if r[0]]
    # Both ids we have ever sent RevenueCat: the backend reconciles with the
    # user UUID, the client has been reporting the lowercased address.
    rc_ids = list(dict.fromkeys(rc_ids + [str(uid), (email or "").strip().lower()]))

    player_id = conn.execute(sa.text(
        "SELECT onesignal_player_id FROM users WHERE id = :uid"
    ), {"uid": uid}).scalar()

    audit_id = uuid.uuid4()
    conn.execute(sa.text("""
        INSERT INTO account_deletion_audit
            (id, subject_hash, actor, requested_at, purged_at,
             revenuecat_status, onesignal_status)
        VALUES
            (:id, :subject_hash, CAST('user' AS deletion_actor), :requested_at, now(),
             CAST('pending' AS propagation_status), CAST('pending' AS propagation_status))
    """), {
        "id": audit_id,
        "subject_hash": subject_hash(uid),
        # When they actually asked. That is what the old column recorded.
        "requested_at": deleted_at,
    })

    conn.execute(sa.text("""
        INSERT INTO account_deletion_propagation
            (id, audit_id, revenuecat_app_user_ids, onesignal_subscription_id,
             onesignal_external_id, attempts, next_attempt_at, created_at)
        VALUES (:id, :audit_id, :rc, :sub, :ext, 0, now(), now())
    """), {
        "id": uuid.uuid4(),
        "audit_id": audit_id,
        "rc": "\n".join(rc_ids) or None,
        "sub": player_id,
        "ext": str(uid),
    })


def _delete_account(conn, uid, email) -> None:
    """Remove every row belonging to one account, children before parents."""
    p = {"uid": uid}

    # Producer content, deepest first. Other people's rows against that content
    # go with it — the same as delete_user.
    conn.execute(sa.text(
        "DELETE FROM downloads WHERE loop_id IN (SELECT id FROM loops WHERE created_by = :uid)"
    ), p)
    conn.execute(sa.text(
        "DELETE FROM likes WHERE loop_id IN (SELECT id FROM loops WHERE created_by = :uid)"
    ), p)
    conn.execute(sa.text(
        "DELETE FROM purchases WHERE loop_id IN (SELECT id FROM loops WHERE created_by = :uid)"
    ), p)
    conn.execute(sa.text("DELETE FROM loops WHERE created_by = :uid"), p)

    packs = "SELECT id FROM stem_packs WHERE created_by = :uid"
    conn.execute(sa.text(
        f"DELETE FROM downloads WHERE stem_id IN "
        f"(SELECT id FROM stems WHERE stem_pack_id IN ({packs}))"
    ), p)
    # Arrangements under this user's packs, plus any they built under someone
    # else's pack — those carry their own created_by and would block the delete.
    arrangements = (
        f"SELECT id FROM stem_arrangements "
        f"WHERE created_by = :uid OR stem_pack_id IN ({packs})"
    )
    conn.execute(sa.text(
        f"DELETE FROM stem_arrangement_tracks WHERE arrangement_id IN ({arrangements})"
    ), p)
    conn.execute(sa.text(
        f"DELETE FROM stem_arrangement_items WHERE arrangement_id IN ({arrangements})"
    ), p)
    conn.execute(sa.text(
        f"DELETE FROM stem_arrangements WHERE created_by = :uid OR stem_pack_id IN ({packs})"
    ), p)
    conn.execute(sa.text(f"DELETE FROM stems WHERE stem_pack_id IN ({packs})"), p)
    conn.execute(sa.text(f"DELETE FROM stem_parts WHERE stem_pack_id IN ({packs})"), p)
    conn.execute(sa.text(f"DELETE FROM purchases WHERE stem_pack_id IN ({packs})"), p)
    conn.execute(sa.text(f"DELETE FROM likes WHERE stem_pack_id IN ({packs})"), p)
    conn.execute(sa.text("DELETE FROM stem_packs WHERE created_by = :uid"), p)

    kits = "SELECT id FROM drum_kits WHERE created_by = :uid"
    conn.execute(sa.text(f"DELETE FROM drum_samples WHERE drum_kit_id IN ({kits})"), p)
    conn.execute(sa.text(f"DELETE FROM downloads WHERE drum_kit_id IN ({kits})"), p)
    conn.execute(sa.text(f"DELETE FROM purchases WHERE drum_kit_id IN ({kits})"), p)
    conn.execute(sa.text("DELETE FROM drum_kits WHERE created_by = :uid"), p)

    # Drones can sit under someone else's category, so collect by both routes —
    # filtering on the category alone strands drones and the users delete then
    # fails on drones_created_by_fkey.
    drones = (
        "SELECT id FROM drones WHERE created_by = :uid OR category_id IN "
        "(SELECT id FROM drone_pad_categories WHERE created_by = :uid)"
    )
    pads = f"SELECT id FROM drone_pads WHERE drone_id IN ({drones})"
    conn.execute(sa.text(f"DELETE FROM downloads WHERE drone_pad_id IN ({pads})"), p)
    conn.execute(sa.text(f"DELETE FROM purchases WHERE drone_pad_id IN ({pads})"), p)
    conn.execute(sa.text(f"DELETE FROM drone_pads WHERE drone_id IN ({drones})"), p)
    conn.execute(sa.text(f"DELETE FROM drones WHERE id IN ({drones})"), p)
    conn.execute(sa.text("DELETE FROM drone_pad_categories WHERE created_by = :uid"), p)

    for table in _OWNED_BY_USER:
        conn.execute(sa.text(f"DELETE FROM {table} WHERE user_id = :uid"), p)

    for table in _OWNED_BY_EMAIL:
        conn.execute(
            sa.text(f"DELETE FROM {table} WHERE lower(email) = :email"),
            {"email": (email or "").lower()},
        )

    conn.execute(sa.text("DELETE FROM users WHERE id = :uid"), p)


def upgrade() -> None:
    conn = op.get_bind()
    pending = conn.execute(sa.text(
        "SELECT id, email, deleted_at FROM users "
        "WHERE deleted_at IS NOT NULL ORDER BY deleted_at"
    )).all()

    if pending:
        print(f"[deletion] finishing {len(pending)} deletion(s) left by the grace period")
    for uid, email, deleted_at in pending:
        orphaned = _s3_keys(conn, uid)
        _queue_third_party_deletion(conn, uid, email, deleted_at)
        _delete_account(conn, uid, email)
        print(f"[deletion] {uid} deleted; third-party removal queued")
        for key in orphaned:
            # Not deletable from here. Printed so it is recoverable work rather
            # than an object nobody knows about.
            print(f"[deletion] ORPHANED S3 OBJECT (delete by hand): {key}")

    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])
