"""fix loops fk on delete

Revision ID: u0v92q71r8s3
Revises: t9u81p60q7r2
Create Date: 2026-05-21
"""
from alembic import op

revision = "u0v92q71r8s3"
down_revision = "t9u81p60q7r2"
branch_labels = None
depends_on = None

_SET_NULL = [
    ("downloads", "downloads_loop_id_fkey", "loop_id"),
    ("purchases", "purchases_loop_id_fkey", "loop_id"),
    ("stem_packs", "stem_packs_loop_id_fkey", "loop_id"),
    ("ai_generations", "ai_generations_result_loop_id_fkey", "result_loop_id"),
]

_CASCADE = [
    ("likes", "likes_loop_id_fkey", "loop_id"),
]


def upgrade() -> None:
    for table, constraint, col in _SET_NULL:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(
            constraint, table, "loops", [col], ["id"], ondelete="SET NULL"
        )

    for table, constraint, col in _CASCADE:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(
            constraint, table, "loops", [col], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    for table, constraint, col in _SET_NULL + _CASCADE:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, "loops", [col], ["id"])
