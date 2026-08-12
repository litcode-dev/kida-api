from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "litmusic",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.notification_tasks",
        "app.tasks.download_tasks",
        "app.tasks.scheduled_tasks",
        "app.tasks.ai_tasks",
        "app.tasks.upload_tasks",
        "app.tasks.render_tasks",
        "app.tasks.price_sync_tasks",
    ],
)

celery_app.conf.broker_connection_retry_on_startup = True

celery_app.conf.beat_schedule = {
    "cleanup-expired-downloads-hourly": {
        "task": "app.tasks.download_tasks.cleanup_expired_downloads",
        "schedule": 3600.0,
    },
    "purge-expired-deleted-accounts-daily": {
        "task": "app.tasks.notification_tasks.purge_expired_deleted_accounts",
        "schedule": 86400.0,
    },
    "reconcile-store-prices": {
        "task": "app.tasks.price_sync_tasks.reconcile_store_prices",
        "schedule": float(settings.price_sync_interval_seconds),
    },
}
celery_app.conf.timezone = "UTC"
