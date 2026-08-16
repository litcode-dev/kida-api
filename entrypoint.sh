#!/bin/sh
set -e

# Migrations run on every boot, and a failure is fatal — a container serving
# traffic against a schema it was not built for is worse than one that will not
# start. The cost is that a migration which refuses to run takes the service
# down with it, and a crash-looping container has no shell to fix it from.
#
# SKIP_MIGRATIONS is the way out of that corner: set it in the dashboard, let
# the service boot, run the migration (or whatever it is asking for) by hand
# from the shell, then remove the variable. Never leave it set.
if [ -n "$SKIP_MIGRATIONS" ]; then
    echo "[entrypoint] SKIP_MIGRATIONS set — skipping migrations. Unset this once the database is up to date."
else
    echo "[entrypoint] Running database migrations..."
    alembic upgrade head
    echo "[entrypoint] Migrations complete."
fi

exec "$@"
