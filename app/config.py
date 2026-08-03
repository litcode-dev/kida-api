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

    # Cloudflare R2 (S3-compatible) — hosts the desktop app installers
    r2_endpoint_url: str = ""  # e.g. https://<account_id>.r2.cloudflarestorage.com
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""

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

    # Email
    email_backend: str = "resend"  # "resend" | "smtp"
    resend_api_key: str = ""
    resend_from: str = "Kida <noreply@litcode.com.ng>"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@kida.litcode.com.ng"

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

    # Apple OAuth (Sign In with Apple)
    apple_client_id: str = ""  # Bundle ID (iOS) or Service ID (web) — used as JWT audience

    # AI Music Generation
    suno_api_key: str = ""
    suno_api_url: str = "https://api.suno.ai"
    ai_selfhosted_url: str = ""
    ai_selfhosted_api_key: str = ""

    # Free-tier download caps (lifetime grants per account for FREE content;
    # subscribers and per-item purchasers are exempt). Tunable without a deploy.
    free_tier_loop_downloads: int = 2
    free_tier_drum_kit_downloads: int = 1
    free_tier_drone_pad_downloads: int = 1
    # Free users may download one whole free drone group (all its keys).
    free_tier_drone_group_downloads: int = 1

    # Subscription pricing (amounts in kobo for Paystack; divide by 100 for Flutterwave major units)
    subscription_monthly_price: int = 200000   # ₦2,000 in kobo
    ai_extra_credits_price: int = 50000        # ₦500 in kobo
    ai_extra_credits_quantity: int = 5         # slots per extra purchase

    # IAP — Apple
    apple_shared_secret: str = ""  # App-specific shared secret from App Store Connect
    # Subscription verification shared secret (falls back to apple_shared_secret)
    app_store_shared_secret: str = ""

    # IAP — Google
    android_package_name: str = "com.litcode.kida"
    google_service_account_json: str = "{}"  # Full service account JSON as a string

    # IAP price sync — Google Play (falls back to google_service_account_json if unset)
    google_play_service_account_json: str = ""

    # IAP price sync — App Store Connect API
    app_store_issuer_id: str = ""
    app_store_key_id: str = ""
    app_store_private_key: str = ""  # PEM contents of the .p8 key (escaped "\n" allowed)
    app_store_app_id: str = ""  # Numeric Apple app ID that owns the in-app purchases
    app_store_review_screenshot_path: str = ""  # Shared review screenshot for new IAPs

    # IAP — RevenueCat
    # Secret REST API key (Project → API keys → "Secret" v1 key). Used to pull a
    # subscriber's real entitlements from RevenueCat on demand.
    revenuecat_api_key: str = ""
    revenuecat_base_url: str = "https://api.revenuecat.com/v1"
    # Entitlement identifier configured in the RevenueCat dashboard that maps to
    # Kiɗa Premium (the app's monthly/yearly products both grant it).
    revenuecat_entitlement_id: str = "premium"
    # Shared secret you set as the webhook "Authorization header" value in the
    # RevenueCat dashboard. Incoming webhooks must present it verbatim. Leaving it
    # blank makes the webhook endpoint reject every request (fail closed).
    revenuecat_webhook_auth_header: str = ""

    # IAP price sync — worker
    price_sync_ngn_per_usd: int = 1600  # USD→NGN rate for the pinned NG region price
    price_sync_interval_seconds: int = 600

    # App installers (desktop download) — object keys within the R2 bucket
    app_installer_macos_s3_key: str = "installers/kida-macos.dmg"
    app_installer_windows_s3_key: str = "installers/kida-windows.exe"

    # Sentry
    sentry_dsn: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
