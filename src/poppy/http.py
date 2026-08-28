import html
import json
import logging
import re
import secrets
from typing import Protocol
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import HttpUrl
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import Lifespan

from poppy.adapters.github_onboarding import GitHubOnboarding, GitHubOnboardingError
from poppy.application.review_cycle import ReviewRequest
from poppy.github_setup import (
    GitHubSetupStore,
    GitHubSetupView,
    github_app_manifest,
    installation_url,
)
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
        <dd>{github_connection}</dd>
        <dt>ChatGPT</dt>
        <dd>Not connected</dd>
      </dl>
    </section>
  </main>
</body>
</html>
"""

GITHUB_MANIFEST_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect GitHub</title>
</head>
<body>
  <main>
    <h1>Continue to GitHub</h1>
    <form action="{action}" method="post">
      <input type="hidden" name="manifest" value="{manifest}">
      <button type="submit">Register Poppy on GitHub</button>
    </form>
  </main>
  <script nonce="{script_nonce}">document.forms[0].submit()</script>
</body>
</html>
"""


class SchedulerPort(Protocol):
    async def schedule(self, request: ReviewRequest) -> bool: ...


def create_setup_app(
    *,
    setup_session: SetupSessionStore,
    github_setup: GitHubSetupStore,
    github_onboarding: GitHubOnboarding,
    public_base_url: HttpUrl,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = _create_base_app(mode="setup", lifespan=lifespan)
    expected_origin = str(public_base_url).rstrip("/")

    @app.middleware("http")
    async def protect_setup_mutations(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        try:
            is_setup_route = request.url.path == "/setup" or request.url.path.startswith("/setup/")
            if is_setup_route and request.method not in {"GET", "HEAD", "OPTIONS"}:
                session_token = request.cookies.get(SETUP_SESSION_COOKIE)
                if request.headers.get("origin") != expected_origin or session_token is None:
                    return _protect_setup_response(
                        JSONResponse({"error": "not found"}, status_code=404)
                    )

                csrf_token = await _setup_form_csrf_token(request)
                if csrf_token is None or not setup_session.mutation_is_authorized(
                    session_token=session_token,
                    csrf_token=csrf_token,
                ):
                    return _protect_setup_response(
                        JSONResponse({"error": "not found"}, status_code=404)
                    )

            response = await call_next(request)
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
                setup_session.confirm_session(session_token) if session_token is not None else None
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
            page = SETUP_PAGE.format(
                csrf_token=html.escape(csrf_token, quote=True),
                github_connection=_github_connection_html(
                    github_setup.view(),
                    csrf_token=csrf_token,
                ),
            )
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

    @app.post("/setup/github/connect")
    async def connect_github() -> Response:
        view = github_setup.view()
        if view.status == "connected":
            return JSONResponse({"error": "GitHub is already connected"}, status_code=409)
        if view.pending is not None:
            return RedirectResponse(installation_url(view.pending), status_code=303)
        correlation_state = github_setup.begin_manifest()
        action = "https://github.com/settings/apps/new?" + urlencode({"state": correlation_state})
        manifest = json.dumps(
            github_app_manifest(expected_origin),
            separators=(",", ":"),
        )
        script_nonce = secrets.token_urlsafe(18)
        response = HTMLResponse(
            GITHUB_MANIFEST_PAGE.format(
                action=html.escape(action, quote=True),
                manifest=html.escape(manifest, quote=True),
                script_nonce=html.escape(script_nonce, quote=True),
            )
        )
        response.headers["content-security-policy"] = (
            "default-src 'none'; base-uri 'none'; "
            "form-action https://github.com; frame-ancestors 'none'; "
            f"script-src 'nonce-{script_nonce}'"
        )
        return response

    @app.get("/setup/github/manifest/callback")
    async def github_manifest_callback(request: Request) -> Response:
        code = _single_query_parameter(request, "code")
        correlation_state = _single_query_parameter(request, "state")
        if (
            code is None
            or correlation_state is None
            or not github_setup.consume_manifest(correlation_state)
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            app_credentials = await github_onboarding.exchange_manifest(code)
        except GitHubOnboardingError:
            logger.warning(
                "GitHub App registration failed",
                extra={"event": "github_manifest_exchange_failed"},
            )
            return JSONResponse(
                {"error": "GitHub App registration failed; return to setup and retry"},
                status_code=502,
            )
        pending = github_setup.await_installation(app_credentials)
        return RedirectResponse(installation_url(pending), status_code=303)

    @app.get("/setup/github/installation")
    async def github_installation_callback(request: Request) -> Response:
        installation_id = _positive_decimal_query_parameter(request, "installation_id")
        correlation_state = _single_query_parameter(request, "state")
        pending = (
            github_setup.consume_installation(correlation_state)
            if correlation_state is not None and installation_id is not None
            else None
        )
        if pending is None or installation_id is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            installation = await github_onboarding.get_installation(
                app=pending.app,
                installation_id=installation_id,
            )
        except GitHubOnboardingError:
            logger.warning(
                "GitHub App installation validation failed",
                extra={"event": "github_installation_validation_failed"},
            )
            return JSONResponse(
                {"error": "GitHub App installation could not be validated; retry from setup"},
                status_code=502,
            )
        if not github_setup.connect(pending=pending, installation=installation):
            return JSONResponse(
                {"error": "GitHub App installation validation became stale"},
                status_code=409,
            )
        return RedirectResponse("/setup", status_code=303)

    return app


def _single_query_parameter(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    return values[0] if len(values) == 1 and values[0] else None


def _positive_decimal_query_parameter(request: Request, name: str) -> int | None:
    value = _single_query_parameter(request, name)
    if value is None or re.fullmatch(r"[1-9][0-9]*", value) is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _github_connection_html(view: GitHubSetupView, *, csrf_token: str) -> str:
    if view.status == "connected":
        return "Connected"
    if view.pending is not None:
        return (
            "Awaiting installation. "
            f'<a href="{html.escape(installation_url(view.pending), quote=True)}">'
            "Continue on GitHub</a>"
        )
    return (
        "Not connected"
        '<form action="/setup/github/connect" method="post">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">'
        '<button type="submit">Connect GitHub</button>'
        "</form>"
    )


async def _setup_form_csrf_token(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "").partition(";")[0]
    if content_type != "application/x-www-form-urlencoded":
        return request.headers.get("x-poppy-csrf-token")

    body = await _read_bounded_body(request, max_bytes=MAX_SETUP_FORM_BYTES)
    if body is None:
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
    if "content-security-policy" not in response.headers:
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

        body = await _read_bounded_body(request, max_bytes=MAX_WEBHOOK_BYTES)
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


async def _read_bounded_body(request: Request, *, max_bytes: int) -> bytes | None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0 or parsed_content_length > max_bytes:
                return None
        except ValueError:
            return None

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    bounded_body = bytes(body)
    request._body = bounded_body
    return bounded_body
