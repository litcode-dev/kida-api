# Kida API

The backend for Kida (LitMusic): a catalogue of loops, stem packs, drum kits and
drone pads, with accounts, subscriptions, payments, downloads and the
notifications that announce new content.

FastAPI on PostgreSQL and Redis, with Celery for background work, S3 (or an
S3-compatible store) for audio, OneSignal for push and Resend or SMTP for mail.

## Running it

Everything runs from `docker compose`, which brings up the API, Postgres, Redis,
a Celery worker and a Celery beat process:

    cp .env.example .env    # then fill in the required values below
    docker compose up

* API — <http://localhost:8000>, everything under `/api/v1`
* Interactive docs — `/docs` (Swagger) and `/redoc`
* Health — `/health`

Migrations run automatically on every boot (see `entrypoint.sh`). A migration
that fails takes the container down with it, which is deliberate: serving
traffic against the wrong schema is worse. `SKIP_MIGRATIONS=1` is the escape
hatch when a container is crash-looping — set it, boot, run `alembic upgrade
head` by hand, then unset it.

Beat matters: it is what fires the daily content digest. A deployment running
only `api` and `worker` looks perfectly healthy while nothing scheduled ever
happens. The API carries an in-process safety net for the digest
(`CONTENT_DIGEST_SCHEDULER_ENABLED`), but beat is the intended sender.

## Tests

    pytest

They run against a real database — `postgresql+asyncpg://litmusic:litmusic@localhost:5432/litmusic_test`
— so create that database first. Redis should also be up: tests that dispatch
Celery tasks otherwise spend their time retrying against a dead broker.

## Configuration

The full list of settings is `app/config.py`, and `.env.example` documents the
ones you are likely to set — read it before deploying. The variables below are
those with no default, and those that change behaviour people notice.

### Required — the app will not start without these

| Variable | What it is |
| --- | --- |
| `SECRET_KEY` | JWT signing secret |
| `DATABASE_URL` | `postgresql+asyncpg://…` |
| `REDIS_URL` | cache, rate limiting, verification codes |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | credentials for the content bucket |
| `S3_BUCKET_NAME` | where audio lives |
| `ONESIGNAL_APP_ID` / `ONESIGNAL_API_KEY` | push notifications |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | background jobs |

Set `S3_ENDPOINT_URL` to put the bucket on an S3-compatible store (R2, MinIO)
instead of AWS.

### New-content announcements

New loops, drum kits, drones and stem packs are announced once a day, as a
roundup, rather than one message per upload. **The email half of that roundup is
switched off by default.**

| Variable | Default | What it does |
| --- | --- | --- |
| `CONTENT_DIGEST_ENABLED` | `true` | The daily roundup as a whole. `false` means no run does anything. |
| `CONTENT_DIGEST_EMAIL_ENABLED` | **`false`** | Whether the roundup is emailed. Off by default: the digest still sweeps, still claims each item once, and goes out as a push alone. Set it to `true` to bring the mail back — nothing else has to change. |
| `CONTENT_DIGEST_PUSH_ENABLED` | `true` | The push copy of the roundup. With the email off this is the only channel, so turning both off leaves a run with nothing to send. |
| `CONTENT_DIGEST_HOUR_UTC` | `17` | When it goes out. 17:00 UTC is 18:00 in Lagos. |
| `CONTENT_DIGEST_SCHEDULER_ENABLED` | `true` | The API's in-process catch-up, for deployments where beat is missing or dead. Set `false` if beat should be the only sender. |
| `CONTENT_DIGEST_CATCH_UP_HOURS` | `6` | How late a missed digest may still go out. |

Nothing is lost while the mail is off: an item announced by push is stamped as
announced exactly as a mailed one is, so switching the email on later does not
re-announce a backlog. Transactional mail — verification, purchases, loop
requests, admin broadcasts — is unaffected by this setting; it only governs the
daily roundup.

`docs/daily-digest.md` has the full picture: what each run records, what happens
when a send or a push fails, and how to investigate when nothing arrived.

### Email delivery

`EMAIL_BACKEND` is `resend` (needs `RESEND_API_KEY`) or `smtp` (needs
`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`). `ADMIN_NOTIFICATION_EMAIL` is the
internal inbox told about signups, deletions and loop requests; blank switches
those off.

### Limits and pricing

Download allowances (`MONTHLY_*_DOWNLOADS`), free-tier grants (`FREE_TIER_*`)
and subscription prices are all environment variables, so they can be tuned
without a deploy. The monthly allowances accept a whole number or `unlimited`;
`0` means zero downloads, not unlimited, and a blank value is rejected rather
than read as "no limit".

## Operations

    python -m scripts.digest_status      # why the digest did or did not go out

Admins can ask the same questions over HTTP: `GET /api/v1/admin/email/digest`
for the schedule, the queue and the last runs, and `POST
/api/v1/admin/email/digest/run` to send one now.

## Where things are

    app/routers/     HTTP endpoints, one module per area
    app/services/    the actual behaviour
    app/models/      SQLAlchemy models
    app/tasks/       Celery tasks and the beat schedule
    alembic/         migrations
    docs/            longer-form notes (start with daily-digest.md)
    scripts/         operational tools
