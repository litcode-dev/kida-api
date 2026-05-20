"""split drones and drone pads

Revision ID: t9u81p60q7r2
Revises: s8t70o59p6q1
Create Date: 2026-05-20
"""
import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "t9u81p60q7r2"
down_revision = "s8t70o59p6q1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drones",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.String(500), nullable=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_free", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("store_product_id", sa.String(255), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "category_id",
            UUID(as_uuid=True),
            sa.ForeignKey("drone_pad_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_drones_category_id", "drones", ["category_id"])

    op.add_column("drone_pads", sa.Column("drone_id", UUID(as_uuid=True), nullable=True))
    op.add_column("drone_pads", sa.Column("preview_url", sa.String(500), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT id, title, price, is_free, store_product_id, download_count,
               category_id, created_by, created_at, thumbnail_s3_key
        FROM drone_pads
        ORDER BY title, key
    """)).mappings().all()

    drone_ids: dict[tuple, uuid.UUID] = {}
    for row in rows:
        key = (
            row["title"],
            row["price"],
            row["is_free"],
            row["store_product_id"],
            row["category_id"],
            row["created_by"],
            row["thumbnail_s3_key"],
        )
        drone_id = drone_ids.get(key)
        if drone_id is None:
            drone_id = uuid.uuid4()
            drone_ids[key] = drone_id
            bind.execute(
                sa.text("""
                    INSERT INTO drones (
                        id, title, thumbnail_url, price, is_free, store_product_id,
                        download_count, category_id, created_by, created_at
                    )
                    VALUES (
                        :id, :title, :thumbnail_url, :price, :is_free, :store_product_id,
                        :download_count, :category_id, :created_by, :created_at
                    )
                """),
                {
                    "id": drone_id,
                    "title": row["title"],
                    "thumbnail_url": row["thumbnail_s3_key"],
                    "price": row["price"],
                    "is_free": row["is_free"],
                    "store_product_id": row["store_product_id"],
                    "download_count": row["download_count"],
                    "category_id": row["category_id"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                },
            )

        bind.execute(
            sa.text("UPDATE drone_pads SET drone_id = :drone_id WHERE id = :pad_id"),
            {"drone_id": drone_id, "pad_id": row["id"]},
        )

    op.alter_column("drone_pads", "drone_id", nullable=False)
    op.create_foreign_key(
        "fk_drone_pads_drone_id",
        "drone_pads",
        "drones",
        ["drone_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_drone_pads_drone_id", "drone_pads", ["drone_id"])

    op.execute(sa.text("DROP INDEX IF EXISTS ix_drone_pads_category_id"))
    op.execute(sa.text(
        "ALTER TABLE drone_pads DROP CONSTRAINT IF EXISTS drone_pads_category_id_fkey"
    ))
    op.execute(sa.text(
        "ALTER TABLE drone_pads DROP CONSTRAINT IF EXISTS drone_pads_created_by_fkey"
    ))
    op.drop_column("drone_pads", "title")
    op.drop_column("drone_pads", "price")
    op.drop_column("drone_pads", "is_free")
    op.drop_column("drone_pads", "store_product_id")
    op.drop_column("drone_pads", "download_count")
    op.drop_column("drone_pads", "category_id")
    op.drop_column("drone_pads", "created_by")


def downgrade() -> None:
    op.add_column("drone_pads", sa.Column("title", sa.String(255), nullable=True))
    op.add_column("drone_pads", sa.Column("price", sa.Numeric(10, 2), nullable=True))
    op.add_column("drone_pads", sa.Column("is_free", sa.Boolean(), nullable=True))
    op.add_column("drone_pads", sa.Column("store_product_id", sa.String(255), nullable=True))
    op.add_column("drone_pads", sa.Column("download_count", sa.Integer(), nullable=True))
    op.add_column("drone_pads", sa.Column("category_id", UUID(as_uuid=True), nullable=True))
    op.add_column("drone_pads", sa.Column("created_by", UUID(as_uuid=True), nullable=True))

    op.execute(sa.text("""
        UPDATE drone_pads dp
        SET title = d.title,
            price = d.price,
            is_free = d.is_free,
            store_product_id = d.store_product_id,
            download_count = d.download_count,
            category_id = d.category_id,
            created_by = d.created_by
        FROM drones d
        WHERE dp.drone_id = d.id
    """))

    op.alter_column("drone_pads", "title", nullable=False)
    op.alter_column("drone_pads", "is_free", nullable=False)
    op.alter_column("drone_pads", "download_count", nullable=False)
    op.alter_column("drone_pads", "created_by", nullable=False)
    op.create_foreign_key(
        "drone_pads_category_id_fkey",
        "drone_pads",
        "drone_pad_categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "drone_pads_created_by_fkey",
        "drone_pads",
        "users",
        ["created_by"],
        ["id"],
    )
    op.create_index("ix_drone_pads_category_id", "drone_pads", ["category_id"])

    op.drop_index("ix_drone_pads_drone_id", table_name="drone_pads")
    op.drop_constraint("fk_drone_pads_drone_id", "drone_pads", type_="foreignkey")
    op.drop_column("drone_pads", "preview_url")
    op.drop_column("drone_pads", "drone_id")

    op.drop_index("ix_drones_category_id", table_name="drones")
    op.drop_table("drones")
