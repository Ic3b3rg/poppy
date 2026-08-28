import html
import logging
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response
from starlette.types import Lifespan

from poppy.application.review_cycle import ReviewRequest
from poppy.setup_session import SetupSessionStore, SetupStateError
from poppy.webhook import InvalidWebhook, normalize_pull_request_webhook, verify_signature

MAX_WEBHOOK_BYTES = 1_000_000
MAX_SETUP_FORM_BYTES = 16_384
SETUP_SESSION_COOKIE = "poppy_setup_session"

logger = logging.getLogger(__name__)

SETUP_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{csrf_token}">
  <title>Set up Poppy</title>
</head>
<body>
  <main>
    <h1>Set up Poppy</h1>
    <p>This protected Setup Session connects your private Poppy Instance.</p>
    <section aria-labelledby="connections-heading">
      <h2 id="connections-heading">Connections</h2>
      <dl>
        <dt>GitHub</dt>
        <dd>Not connected</dd>
        <dt>ChatGPT</dt>
        <dd>Not connected</dd>
      </dl>
    </section>
  </main>
</body>
</html>
"""


class SchedulerPort(Protocol):
    async def schedule(self, request: ReviewRequest) -> bool: ...


def create_setup_app(
    *,
    setup_session: SetupSessionStore,
    public_base_url: str,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = _create_base_app(mode="setup", lifespan=lifespan)
    parsed_base_url = urlsplit(public_base_url)
    expected_origin = f"{parsed_base_url.scheme}://{parsed_base_url.netloc}"

    @app.middleware("http")
    async def protect_setup_mutations(request: Request, call_next: object) -> Response:
        try:
            is_setup_route = request.url.path == "/setup" or request.url.path.startswith("/setup/")
            if is_setup_route and request.method not in {"GET", "HEAD", "OPTIONS"}:
                session_token = request.cookies.get(SETUP_SESSION_COOKIE)
                csrf_token = await _setup_form_csrf_token(request)
                if (
                    request.headers.get("origin") != expected_origin
                    or session_token is None
                    or csrf_token is None
                    or not setup_session.mutation_is_authorized(
                        session_token=session_token,
                        csrf_token=csrf_token,
                    )
                ):
                    return _protect_setup_response(
                        JSONResponse({"error": "not found"}, status_code=404)
                    )

            response = await call_next(request)  # type: ignore[operator]
            return _protect_setup_response(response)
        except SetupStateError:
            logger.exception(
                "Setup Session state is unavailable",
                extra={"event": "setup_state_unavailable"},
            )
            return _protect_setup_response(
                JSONResponse({"error": "setup unavailable"}, status_code=503)
            )

    @app.get("/setup")
    async def setup(request: Request) -> Response:
        token = request.query_params.get("token")
        if token is None:
            session_token = request.cookies.get(SETUP_SESSION_COOKIE)
            csrf_token = (
                setup_session.csrf_token_for(session_token) if session_token is not None else None
            )
            if session_token is None or csrf_token is None:
                unavailable_response = JSONResponse({"error": "not found"}, status_code=404)
                if session_token is not None:
                    unavailable_response.delete_cookie(
                        SETUP_SESSION_COOKIE,
                        path="/setup",
                        secure=True,
                        httponly=True,
                        samesite="strict",
                    )
                return _protect_setup_response(unavailable_response)
            page = SETUP_PAGE.format(csrf_token=html.escape(csrf_token, quote=True))
            return _protect_setup_response(HTMLResponse(page))

        grant = setup_session.exchange_bootstrap(token)
        if grant is None:
            return _protect_setup_response(JSONResponse({"error": "not found"}, status_code=404))

        response = RedirectResponse(url="/setup", status_code=303)
        response.set_cookie(
            key=SETUP_SESSION_COOKIE,
            value=grant.token,
            max_age=grant.max_age,
            path="/setup",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return _protect_setup_response(response)

    @app.post("/webhooks/github")
    async def unavailable_github_webhook() -> JSONResponse:
        return JSONResponse({"error": "reviewer unavailable"}, status_code=503)

    return app


async def _setup_form_csrf_token(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "").partition(";")[0]
    if content_type != "application/x-www-form-urlencoded":
        return request.headers.get("x-poppy-csrf-token")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_SETUP_FORM_BYTES:
                return None
        except ValueError:
            return None
    body = await request.body()
    if len(body) > MAX_SETUP_FORM_BYTES:
        return None
    try:
        values = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=32,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    csrf_tokens = values.get("csrf_token", [])
    return csrf_tokens[0] if len(csrf_tokens) == 1 else None


def _protect_setup_response(response: Response) -> Response:
    response.headers["cache-control"] = "no-store"
    response.headers["content-security-policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["strict-transport-security"] = "max-age=31536000"
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-frame-options"] = "DENY"
    return response


def create_app(
    *,
    webhook_secret: bytes,
    scheduler: SchedulerPort,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = _create_base_app(mode="reviewer", lifespan=lifespan)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type != "application/json":
            return JSONResponse({"error": "unsupported content type"}, status_code=415)

        body = await _read_bounded_body(request)
        if body is None:
            return JSONResponse({"error": "webhook body too large"}, status_code=413)
        if not verify_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            webhook_secret,
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if request.headers.get("x-github-event") != "pull_request":
            return JSONResponse({"status": "ignored"}, status_code=202)

        delivery_id = request.headers.get("x-github-delivery")
        if not delivery_id:
            return JSONResponse({"error": "missing delivery id"}, status_code=400)
        try:
            review_request = normalize_pull_request_webhook(body, delivery_id=delivery_id)
        except InvalidWebhook:
            return JSONResponse({"error": "invalid webhook payload"}, status_code=400)

        if review_request is None:
            return JSONResponse({"status": "ignored"}, status_code=202)
        scheduled = await scheduler.schedule(review_request)
        status = "scheduled" if scheduled else "duplicate"
        return JSONResponse({"status": status}, status_code=202)

    return app


def _create_base_app(*, mode: str, lifespan: Lifespan[FastAPI] | None) -> FastAPI:
    app = FastAPI(title="Poppy", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": mode}

    return app


async def _read_bounded_body(request: Request) -> bytes | None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_BYTES:
                return None
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)
