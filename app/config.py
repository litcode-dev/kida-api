from pydantic import field_validator
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
    # Empty means AWS S3. Set it to put the content bucket on an S3-compatible
    # store instead — Cloudflare R2, MinIO, LocalStack. The credentials above
    # are then that store's, not AWS's.
    s3_endpoint_url: str = ""  # e.g. https://<account_id>.r2.cloudflarestorage.com
    # Whatever serves the bucket publicly: a CloudFront distribution, or an R2
    # custom domain / r2.dev URL.
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

    # Squad (GTCO). Sandbox base: https://sandbox-api-d.squadco.com
    squad_secret_key: str = ""
    squad_base_url: str = "https://api-d.squadco.com"

    # Stripe. The webhook secret is the endpoint's signing secret (whsec_...),
    # which is not the API key.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Frontend
    frontend_url: str = "https://litmusic.app"

    # The Kida website, as every email link and footer spells it. One setting
    # rather than the same literal in fifteen templates.
    site_url: str = "https://kida.litcode.com.ng"

    # Where an email CTA sends someone who should end up in the mobile app.
    # An https:// URL, not a kida:// scheme, so it is a Universal Link on iOS
    # and an App Link on Android: the OS hands it to the app when it is
    # installed and the browser opens it as an ordinary page when it is not,
    # which means the button works either way.
    #
    # It only starts opening the app once kida.litcode.com.ng serves
    # apple-app-site-association and assetlinks.json covering this path. Until
    # then it behaves exactly like the plain web link it defaults to.
    app_deep_link_url: str = "https://kida.litcode.com.ng"

    # New-content digest
    # One email a day listing everything that went live, instead of one email
    # per item to every recipient. Push notifications still fire per item, so
    # nothing here delays the "it's live" signal — this is the roundup.
    content_digest_enabled: bool = True
    # The digest also goes out as one push to every subscribed device, sent
    # with the mail and saying the same thing. Most of the audience is on the
    # app rather than in an inbox, and a stem pack the producer publishes days
    # after upload has no per-item push left to fire — the roundup is the only
    # announcement it gets. Turn it off to leave the digest email-only; the
    # mail sends either way, because a failed push must not cost the roundup.
    content_digest_push_enabled: bool = True
    # Hour (UTC) the digest is sent. 17:00 UTC is 18:00 in Lagos — evening,
    # after work, for the audience this catalogue is built for.
    content_digest_hour_utc: int = 17
    # How many addresses go in one provider request. Resend's batch endpoint
    # caps at 100.
    email_batch_size: int = 100
    # The API process also checks whether the day's digest has gone out, and
    # sends it if nothing else did. Celery beat is a separate process that
    # serves no traffic, so a deployment missing it looks perfectly healthy
    # while no daily mail is ever sent — this is the safety net for that.
    # Runs are claimed through a unique row in digest_runs, so beat, several API
    # replicas and a manual trigger cannot between them send two copies.
    content_digest_scheduler_enabled: bool = True
    content_digest_scheduler_interval_seconds: int = 300
    # How late a missed digest may still go out. Catching up an hour after the
    # scheduled time is a fix; delivering "today's drops" at 4am is not, so
    # beyond this the run is left for the next day's slot.
    content_digest_catch_up_hours: int = 6

    # Email
    email_backend: str = "resend"  # "resend" | "smtp"
    resend_api_key: str = ""
    resend_from: str = "Kida <noreply@litcode.com.ng>"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@kida.litcode.com.ng"
    # Internal inbox notified when a new account is created. Blank disables the
    # notification without a code change.
    admin_notification_email: str = "kida.audio@gmail.com"

    # OneSignal
    onesignal_app_id: str
    onesignal_api_key: str

    # Account deletion
    # How long a confirmation link from the public deletion page stays valid.
    # Short: it authorises destroying an account, and the person is reading the
    # email they just asked for.
    deletion_request_token_ttl_minutes: int = 60
    # Attempts before a third-party deletion (RevenueCat, OneSignal) is given up
    # on and recorded as failed. With the exponential backoff in
    # account_deletion_service this spans roughly a day and a half.
    deletion_propagation_max_attempts: int = 8

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

    # Monthly download allowance, counted per distinct item per calendar month
    # (UTC). Applies to every account, on top of the free-tier caps below.
    #
    # Each accepts either a whole number or "unlimited" (also "none" / "off"),
    # which parses to None and removes the cap for that type. 0 means zero
    # downloads allowed, not unlimited. A blank value is rejected rather than
    # read as unlimited — a stray "MONTHLY_LOOP_DOWNLOADS=" should fail loudly,
    # not silently uncap downloads.
    monthly_loop_downloads: int | None = 20
    monthly_drone_downloads: int | None = 5
    monthly_drum_kit_downloads: int | None = 5

    @field_validator(
        "monthly_loop_downloads", "monthly_drone_downloads", "monthly_drum_kit_downloads",
        mode="before",
    )
    @classmethod
    def parse_allowance(cls, v):
        if isinstance(v, str):
            token = v.strip().lower()
            if token in ("unlimited", "none", "off"):
                return None
            if token == "":
                raise ValueError(
                    "must be a whole number or 'unlimited' — leave the variable "
                    "unset to use the default"
                )
            v = token
        parsed = int(v)
        if parsed < 0:
            raise ValueError("must be zero or greater, or 'unlimited'")
        return parsed

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
    # Extra downloads sold once a user's monthly allowance is spent.
    download_extra_credits_price: int = 50000  # ₦500 in kobo
    download_extra_credits_quantity: int = 10  # downloads per purchase

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
