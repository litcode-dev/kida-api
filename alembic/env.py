import asyncio
from logging.config import fileConfig
import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
from app.database import Base
from app.config import get_settings
import app.models  # noqa: F401 — ensure all models are imported

config = context.config
settings = get_settings()
_db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    def on_version_apply(ctx, step, heads, run_args):
        direction = "upgrade" if step.is_upgrade else "downgrade"
        revision = (
            step.up_revision
            if step.is_upgrade
            else (", ".join(str(r) for r in step.down_revisions) if step.down_revisions else "base")
        )
        description = getattr(step, "doc", None) or revision
        try:
            connection.execute(sa.text("SAVEPOINT migration_log_sp"))
            connection.execute(
                sa.text(
                    "INSERT INTO migration_log (revision, description, direction) "
                    "VALUES (:rev, :desc, :dir)"
                ),
                {"rev": revision, "desc": description, "dir": direction},
            )
            connection.execute(sa.text("RELEASE SAVEPOINT migration_log_sp"))
        except Exception:
            connection.execute(sa.text("ROLLBACK TO SAVEPOINT migration_log_sp"))

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        on_version_apply=on_version_apply,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
