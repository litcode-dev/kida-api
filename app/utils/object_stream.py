import httpx
import structlog
from fastapi.responses import StreamingResponse

from app.exceptions import AppError

log = structlog.get_logger()


async def stream_stored_object(
    url: str, media_type: str, missing_message: str
) -> StreamingResponse:
    """Stream an object out of the bucket, without passing an error off as audio.

    The response used to be streamed straight through whatever came back. A key
    the bucket does not hold — which is every key, after a migration that moved
    the database but not the objects — therefore reached the player as the
    store's XML error body, under a 200 and an audio content type. Nothing
    downstream could tell that from a file, so a missing object surfaced as a
    player that failed for no stated reason.

    The status is read before the response starts: once the first chunk is out,
    the 200 has been sent and there is no taking it back.
    """
    client = httpx.AsyncClient()
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
    except Exception:
        await client.aclose()
        raise

    if response.status_code != 200:
        status_code = response.status_code
        await response.aclose()
        await client.aclose()
        if status_code == 404:
            raise AppError(
                missing_message, status_code=409, data={"reason": "missing_object"}
            )
        # Anything else is the store refusing us rather than the object being
        # absent — a signature the bucket rejects, say. That is ours to fix, and
        # must not be reported as though the upload were at fault.
        log.error("preview_store_unavailable", status_code=status_code)
        raise AppError("The audio store could not be reached", status_code=502)

    async def _stream():
        try:
            async for chunk in response.aiter_bytes(8192):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(_stream(), media_type=media_type)
