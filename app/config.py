from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    secret_key: str
    api_base_url: str = ""  # e.g. https://litmusic-api-production.up.railway.app
    # Stored as a raw string so pydantic-settings doesn't try to JSON-decode it.
    # Accepts comma-separated ("a,b") or JSON array ('["a","b"]') — parsed by parse_allowed_origins().
    allowed_origins: str = "http://localhost:3000"

    # Database
    database_url: str

    # Redis
    redis_url: str

    # AWS S3
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"
    s3_bucket_name: str
    s3_cloudfront_url: str = ""  # e.g. https://d2q7nhojr9v45l.cloudfront.net

    # Flutterwave
    flw_secret_key: str = ""
    flw_hash: str = ""  # Webhook verification hash (set in Flutterwave dashboard)
    flutterwave_public_key: str = ""
    flutterwave_secret_key: str = ""
    flutterwave_secret_hash: str = ""
    flutterwave_base_url: str = "https://api.flutterwave.com/v3"

    # Paystack
    paystack_secret_key: str = ""

    # Frontend
    frontend_url: str = "https://litmusic.app"

    # Email (SMTP)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@litmusic.app"

    # OneSignal
    onesignal_app_id: str
    onesignal_api_key: str

    # JWT
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # Celery
    celery_broker_url: str
    celery_result_backend: str

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:3000/auth/google/callback"

    # AI Music Generation
    suno_api_key: str = ""
    suno_api_url: str = "https://api.suno.ai"
    ai_selfhosted_url: str = ""
    ai_selfhosted_api_key: str = ""

    # Subscription pricing (amounts in kobo for Paystack; divide by 100 for Flutterwave major units)
    subscription_monthly_price: int = 200000   # ₦2,000 in kobo
    ai_extra_credits_price: int = 50000        # ₦500 in kobo
    ai_extra_credits_quantity: int = 5         # slots per extra purchase

    # IAP — Apple
    apple_shared_secret: str = ""  # App-specific shared secret from App Store Connect

    # IAP — Google
    android_package_name: str = "com.litcode.kida"
    google_service_account_json: str = "{}"  # Full service account JSON as a string


@lru_cache
def get_settings() -> Settings:
    return Settings()
