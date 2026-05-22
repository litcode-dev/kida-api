"""add missing indexes

Revision ID: x3y25t04u1v6
Revises: w2x14s93t0u5
Create Date: 2026-05-22
"""
from alembic import op

revision = "x3y25t04u1v6"
down_revision = "w2x14s93t0u5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_users_onesignal_player_id", "users", ["onesignal_player_id"], if_not_exists=True)
    op.create_index("ix_loops_created_by", "loops", ["created_by"], if_not_exists=True)
    op.create_index("ix_stem_packs_loop_id", "stem_packs", ["loop_id"], if_not_exists=True)
    op.create_index("ix_stem_packs_created_by", "stem_packs", ["created_by"], if_not_exists=True)
    op.create_index("ix_stems_stem_pack_id", "stems", ["stem_pack_id"], if_not_exists=True)
    op.create_index("ix_drum_kits_created_by", "drum_kits", ["created_by"], if_not_exists=True)
    op.create_index("ix_drum_samples_drum_kit_id", "drum_samples", ["drum_kit_id"], if_not_exists=True)
    op.create_index("ix_downloads_loop_id", "downloads", ["loop_id"], if_not_exists=True)
    op.create_index("ix_downloads_stem_id", "downloads", ["stem_id"], if_not_exists=True)
    op.create_index("ix_downloads_drum_kit_id", "downloads", ["drum_kit_id"], if_not_exists=True)
    op.create_index("ix_downloads_drone_pad_id", "downloads", ["drone_pad_id"], if_not_exists=True)
    op.create_index("ix_likes_loop_id", "likes", ["loop_id"], if_not_exists=True)
    op.create_index("ix_likes_stem_pack_id", "likes", ["stem_pack_id"], if_not_exists=True)
    op.create_index("ix_ai_generations_subscription_id", "ai_generations", ["subscription_id"], if_not_exists=True)
    op.create_index("ix_ai_generations_result_loop_id", "ai_generations", ["result_loop_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_ai_generations_result_loop_id", table_name="ai_generations")
    op.drop_index("ix_ai_generations_subscription_id", table_name="ai_generations")
    op.drop_index("ix_likes_stem_pack_id", table_name="likes")
    op.drop_index("ix_likes_loop_id", table_name="likes")
    op.drop_index("ix_downloads_drone_pad_id", table_name="downloads")
    op.drop_index("ix_downloads_drum_kit_id", table_name="downloads")
    op.drop_index("ix_downloads_stem_id", table_name="downloads")
    op.drop_index("ix_downloads_loop_id", table_name="downloads")
    op.drop_index("ix_drum_samples_drum_kit_id", table_name="drum_samples")
    op.drop_index("ix_drum_kits_created_by", table_name="drum_kits")
    op.drop_index("ix_stems_stem_pack_id", table_name="stems")
    op.drop_index("ix_stem_packs_created_by", table_name="stem_packs")
    op.drop_index("ix_stem_packs_loop_id", table_name="stem_packs")
    op.drop_index("ix_loops_created_by", table_name="loops")
    op.drop_index("ix_users_onesignal_player_id", table_name="users")
