from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.exceptions import unexpected_error_response


class UnhandledErrorMiddleware:
    """Answer an unhandled exception here, where CORS can still see the response.

    Starlette's own catch-all lives in ServerErrorMiddleware, which wraps
    everything — CORSMiddleware included — so the 500 it writes carries no
    Access-Control-Allow-Origin header. The browser then refuses to hand the
    response to the caller at all: the admin dashboard read a bug in an
    endpoint as "blocked by CORS policy", with no status and no message to show
    or report, which sends whoever is debugging it to the origin list instead
    of the traceback.

    Registered inside the CORS layer, so the envelope and the headers travel
    together. Pure ASGI, like LoggingMiddleware, to keep the response body
    unbuffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Once the status line is out there is no response left to replace;
            # let ServerErrorMiddleware close the connection as it always has.
            if response_started:
                raise
            # Sentry reports what reaches ServerErrorMiddleware, which this no
            # longer does, so the capture has to happen here.
            response = unexpected_error_response(Request(scope, receive), exc, capture=True)
            await response(scope, receive, send)
