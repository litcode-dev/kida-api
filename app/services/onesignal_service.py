import httpx
import structlog
from app.config import get_settings

log = structlog.get_logger()

ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"


def _headers() -> dict:
    settings = get_settings()
    return {
        "Authorization": f"Basic {settings.onesignal_api_key}",
        "Content-Type": "application/json",
    }


def _app_id() -> str:
    return get_settings().onesignal_app_id


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


async def send_notification(
    player_id: str,
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
) -> dict:
    """Send a push notification to a single device."""
    payload = {
        "app_id": _app_id(),
        "include_player_ids": [player_id],
        "headings": {"en": title},
        "contents": {"en": message},
        "data": data or {},
    }
    if image_url:
        payload["big_picture"] = image_url
        payload["ios_attachments"] = {"image": image_url}
    return await _post(payload)


async def send_to_users(
    player_ids: list[str],
    title: str,
    message: str,
    data: dict | None = None,
    image_url: str | None = None,
) -> dict:
    """Send to a specific list of player IDs."""
    payload = {
        "app_id": _app_id(),
        "include_player_ids": player_ids,
        "headings": {"en": title},
        "contents": {"en": message},
        "data": data or {},
    }
    if image_url:
        payload["big_picture"] = image_url
        payload["ios_attachments"] = {"image": image_url}
    return await _post(payload)


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


# ── Transactional helpers ─────────────────────────────────────────────────────

async def send_purchase_confirmation_notification(player_id: str, loop_title: str) -> dict:
    return await send_notification(
        player_id=player_id,
        title="Purchase Successful!",
        message=f'You now own "{loop_title}". Download it anytime.',
        data={"type": "purchase_confirmation"},
    )


async def send_new_loop_notification(player_id: str, genre: str, loop_title: str, loop_id: str) -> dict:
    return await send_notification(
        player_id=player_id,
        title=f"New {genre} Loop!",
        message=f'"{loop_title}" just dropped. Check it out.',
        data={"type": "new_loop", "loop_id": loop_id},
    )
