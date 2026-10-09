from urllib.parse import quote

import httpx
import structlog
from app.config import get_settings

log = structlog.get_logger()

ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"

# The user/subscription endpoints live on the newer host and use "Key" auth
# rather than the legacy "Basic" the notifications endpoint above still takes.
ONESIGNAL_BASE_URL = "https://api.onesignal.com"


def _headers() -> dict:
    settings = get_settings()
    return {
        "Authorization": f"Basic {settings.onesignal_api_key}",
        "Content-Type": "application/json",
    }


def _key_headers() -> dict:
    settings = get_settings()
    return {
        "Authorization": f"Key {settings.onesignal_api_key}",
        "Content-Type": "application/json",
    }


def _app_id() -> str:
    return get_settings().onesignal_app_id


def delivery_problem(settings=None) -> str | None:
    """Why a push would go nowhere, or None if OneSignal is usable.

    The digest asks this before it sends, for the same reason the mail backend
    is checked: a broadcast against missing credentials is a 400 nobody reads,
    and "the push never arrived" should name its cause on the run row rather
    than only in a log line.
    """
    settings = settings or get_settings()
    missing = [
        name
        for name, value in (
            ("ONESIGNAL_APP_ID", settings.onesignal_app_id),
            ("ONESIGNAL_API_KEY", settings.onesignal_api_key),
        )
        if not value
    ]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return f"{'/'.join(missing)} {verb} empty; OneSignal would reject the send"
    return None


async def _post(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(ONESIGNAL_API_URL, json=payload, headers=_headers())
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code not in (200, 201):
        log.error("onesignal.failed", status=resp.status_code, body=body)
    return {"status_code": resp.status_code, "body": body}


def _content(
    title: str, message: str, data: dict | None, image_url: str | None
) -> dict:
    content = {
        "headings": {"en": title},
        "contents": {"en": message},
        "data": data or {},
    }
    if image_url:
        content["big_picture"] = image_url
        content["ios_attachments"] = {"image": image_url}
    return content


# How many ids go in one request; OneSignal caps how many a single
# notification may name, so a long list is sent in batches.
SEND_BATCH = 2000


async def _post_targeted(target: dict, content: dict) -> dict:
    payload = {"app_id": _app_id(), "target_channel": "push", **target, **content}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ONESIGNAL_BASE_URL}/notifications", json=payload, headers=_key_headers()
        )
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code not in (200, 201):
        log.error("onesignal.failed", status=resp.status_code, body=body)
    return {"status_code": resp.status_code, "body": body}


def _unreached(sent: list[str], result: dict) -> list[str]:
    """Which of ``sent`` OneSignal could not deliver to.

    It answers 200 either way: a partial miss names the unknown ids under
    ``errors.invalid_aliases`` (or ``errors.invalid_player_ids`` for
    subscription ids), and a total miss comes back with an empty ``id`` and
    a list of errors such as "All included players are not subscribed".
    """
    body = result["body"] if isinstance(result["body"], dict) else {}
    if result["status_code"] not in (200, 201) or not body.get("id"):
        return list(sent)
    errors = body.get("errors")
    if not isinstance(errors, dict):
        return []
    invalid = set()
    aliases = errors.get("invalid_aliases")
    if isinstance(aliases, dict):
        invalid.update(aliases.get("external_id") or [])
    invalid.update(errors.get("invalid_player_ids") or [])
    return [i for i in sent if i in invalid]


async def send_to_external_ids(
    external_ids: list[str],
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
    fallback_subscription_ids: dict[str, str] | None = None,
) -> dict:
    """Push to users by the ``external_id`` the app sets with ``OneSignal.login``.

    One external id reaches every device the user is signed in on. For a user
    OneSignal does not know by external id yet (an app build that predates the
    login call), ``fallback_subscription_ids`` maps their external id to the
    device id they registered at /push/register-device, and that one device
    is tried instead.

    Returns the OneSignal responses, how many users were reached each way, and
    the external ids that could not be reached at all.
    """
    content = _content(title, message, data, image_url)
    fallback = fallback_subscription_ids or {}
    responses = []

    unknown: list[str] = []
    for i in range(0, len(external_ids), SEND_BATCH):
        batch = external_ids[i:i + SEND_BATCH]
        result = await _post_targeted({"include_aliases": {"external_id": batch}}, content)
        responses.append(result)
        unknown.extend(_unreached(batch, result))

    retry = {uid: fallback[uid] for uid in unknown if fallback.get(uid)}
    unreachable = [uid for uid in unknown if uid not in retry]
    fallback_missed = 0
    by_subscription = list(retry.items())
    for i in range(0, len(by_subscription), SEND_BATCH):
        batch = by_subscription[i:i + SEND_BATCH]
        sub_ids = [sub for _, sub in batch]
        result = await _post_targeted({"include_subscription_ids": sub_ids}, content)
        responses.append(result)
        missed = set(_unreached(sub_ids, result))
        missed_users = [uid for uid, sub in batch if sub in missed]
        unreachable.extend(missed_users)
        fallback_missed += len(missed_users)

    return {
        "responses": responses,
        "by_external_id": len(external_ids) - len(unknown),
        "by_subscription_id": len(retry) - fallback_missed,
        "unreachable": unreachable,
    }


async def send_to_user(
    user_id: str,
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
    subscription_id: str | None = None,
) -> dict:
    """Push to one user's devices; ``subscription_id`` is the fallback device."""
    return await send_to_external_ids(
        [user_id], title, message, data=data, image_url=image_url,
        fallback_subscription_ids={user_id: subscription_id} if subscription_id else None,
    )


async def send_to_all(
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
) -> dict:
    """Broadcast to all subscribed devices."""
    payload = {
        "app_id": _app_id(),
        "included_segments": ["All"],
        "headings": {"en": title},
        "contents": {"en": message},
        "data": data or {},
    }
    if image_url:
        payload["big_picture"] = image_url
        payload["ios_attachments"] = {"image": image_url}
    return await _post(payload)


async def send_to_segment(
    segment: str,
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
) -> dict:
    """Send to a named OneSignal segment (e.g. 'Active Users')."""
    payload = {
        "app_id": _app_id(),
        "included_segments": [segment],
        "headings": {"en": title},
        "contents": {"en": message},
        "data": data or {},
    }
    if image_url:
        payload["big_picture"] = image_url
        payload["ios_attachments"] = {"image": image_url}
    return await _post(payload)


# ── Deletion ──────────────────────────────────────────────────────────────────

async def _delete(path: str, what: str) -> bool:
    """DELETE against the OneSignal user API. True when the record is gone.

    A 404 counts as gone: "never existed" and "already deleted" are the same end
    state, and a retried deletion must not report failure the second time.
    Returns False for anything worth retrying (429, 5xx, network). Raises for a
    permanent 4xx so the caller stops retrying instead of looping.
    """
    settings = get_settings()
    if not settings.onesignal_api_key or not settings.onesignal_app_id:
        raise RuntimeError("OneSignal is not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{ONESIGNAL_BASE_URL}{path}", headers=_key_headers()
            )
    except httpx.HTTPError as exc:
        log.warning("onesignal.delete_transport_error", what=what, error=str(exc))
        return False

    if resp.status_code in (200, 202, 204, 404):
        log.info("onesignal.deleted", what=what, status=resp.status_code)
        return True
    if resp.status_code == 429 or resp.status_code >= 500:
        log.warning("onesignal.delete_retryable", what=what, status=resp.status_code)
        return False
    log.error(
        "onesignal.delete_failed",
        what=what, status=resp.status_code, body=resp.text[:300],
    )
    raise RuntimeError(f"OneSignal rejected the {what} delete ({resp.status_code})")


async def delete_user_by_external_id(external_id: str) -> bool:
    """Delete a OneSignal user and every channel subscription they hold.

    Keyed on the ``external_id`` alias, which is what the app sets when it calls
    ``OneSignal.login(user.id)``. This is the thorough one — it covers every
    device the person ever installed on, not just the last one to register.
    """
    app_id = get_settings().onesignal_app_id
    return await _delete(
        f"/apps/{app_id}/users/by/external_id/{quote(external_id, safe='')}",
        "user",
    )


async def delete_subscription(subscription_id: str) -> bool:
    """Delete a single push subscription by its id.

    ``users.onesignal_player_id`` holds what the legacy SDK called a player id;
    OneSignal's current model calls the same value a subscription id. This is
    the fallback for accounts where no ``external_id`` alias was ever set, and
    it only removes the one device it names.
    """
    app_id = get_settings().onesignal_app_id
    return await _delete(
        f"/apps/{app_id}/subscriptions/{quote(subscription_id, safe='')}",
        "subscription",
    )


# ── Transactional helpers ─────────────────────────────────────────────────────

async def send_purchase_confirmation_notification(
    user_id: str, loop_title: str, subscription_id: str | None = None
) -> dict:
    return await send_to_user(
        user_id,
        title="Purchase Successful!",
        message=f'You now own "{loop_title}". Download it anytime.',
        data={"type": "purchase_confirmation"},
        subscription_id=subscription_id,
    )


async def send_new_loop_notification(
    user_ids: list[str],
    genre: str,
    loop_title: str,
    loop_id: str,
    fallback_subscription_ids: dict[str, str] | None = None,
) -> dict:
    return await send_to_external_ids(
        user_ids,
        title=f"New {genre} Loop!",
        message=f'"{loop_title}" just dropped. Check it out.',
        data={"type": "new_loop", "loop_id": loop_id},
        fallback_subscription_ids=fallback_subscription_ids,
    )
