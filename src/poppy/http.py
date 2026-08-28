import html
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import HttpUrl
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import Lifespan

from poppy.adapters.codex_onboarding import CodexOnboardingError
from poppy.adapters.github_onboarding import GitHubOnboarding, GitHubOnboardingError
from poppy.chatgpt_setup import (
    ChatGPTAuthorizationStart,
    ChatGPTPendingView,
    ChatGPTSetup,
    ChatGPTSetupView,
)
from poppy.github_setup import (
    GitHubOrganization,
    GitHubSetupStore,
    GitHubSetupView,
    github_app_manifest,
    installation_url,
    manifest_registration_url,
)
from poppy.runtime import (
    ActiveReviewer,
    RuntimeMode,
    RuntimeStateError,
    SchedulerPort,
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
  {chatgpt_refresh}
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
        <dd>
          {chatgpt_connection}
          <p>Connect a ChatGPT account. API keys are not supported.</p>
        </dd>
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


class ReviewerRuntimePort(Protocol):
    @property
    def mode(self) -> RuntimeMode: ...

    def active_reviewer(self) -> ActiveReviewer | None: ...


class SetupRuntimePort(ReviewerRuntimePort, Protocol):
    @property
    def setup_closed(self) -> bool: ...

    def setup_access(self) -> AbstractAsyncContextManager[bool]: ...

    async def promote_if_ready(self) -> bool: ...


def create_setup_app(
    *,
    setup_session: SetupSessionStore,
    github_setup: GitHubSetupStore,
    github_onboarding: GitHubOnboarding,
    chatgpt_setup: ChatGPTSetup,
    public_base_url: HttpUrl,
    reviewer_runtime: SetupRuntimePort | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    runtime: SetupRuntimePort = reviewer_runtime or _UnavailableSetupRuntime()
    app = _create_base_app(mode=lambda: runtime.mode, lifespan=lifespan)
    _register_github_webhook(app, runtime)
    expected_origin = str(public_base_url).rstrip("/")

    @app.middleware("http")
    async def protect_setup_mutations(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        try:
            is_setup_route = request.url.path == "/setup" or request.url.path.startswith("/setup/")
            if not is_setup_route:
                return _protect_setup_response(await call_next(request))

            async with runtime.setup_access() as setup_available:
                if not setup_available:
                    return _protect_setup_response(
                        JSONResponse({"error": "not found"}, status_code=404)
                    )
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
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

            should_promote = getattr(request.state, "promote_reviewer", False) or (
                request.url.path == "/setup" and response.status_code == 200
            )
            if should_promote:
                try:
                    await runtime.promote_if_ready()
                except RuntimeStateError:
                    logger.warning(
                        "Poppy reviewer promotion failed",
                        extra={"event": "reviewer_promotion_failed"},
                    )

            async with runtime.setup_access() as setup_available:
                if not setup_available:
                    response = JSONResponse({"error": "not found"}, status_code=404)
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
            github_view = github_setup.view()
            chatgpt_view = chatgpt_setup.view()
            page = SETUP_PAGE.format(
                csrf_token=html.escape(csrf_token, quote=True),
                chatgpt_refresh=(
                    '<meta http-equiv="refresh" content="2; url=/setup">'
                    if chatgpt_view.status == "pending"
                    or (
                        runtime.mode == "setup"
                        and github_view.status == "connected"
                        and chatgpt_view.status == "connected"
                    )
                    else ""
                ),
                github_connection=_github_connection_html(
                    github_view,
                    csrf_token=csrf_token,
                ),
                chatgpt_connection=_chatgpt_connection_html(
                    chatgpt_view,
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

    @app.post("/setup/github/connect")
    async def connect_github(request: Request) -> Response:
        try:
            organization = _github_organization(request)
        except ValueError:
            return JSONResponse({"error": "invalid GitHub organization"}, status_code=400)
        view = github_setup.view()
        if view.status == "connected":
            return JSONResponse({"error": "GitHub is already connected"}, status_code=409)
        if view.pending is not None:
            return RedirectResponse(installation_url(view.pending), status_code=303)
        registration = github_setup.begin_manifest(
            organization=organization,
            public_origin=expected_origin,
        )
        if registration is None:
            return JSONResponse(
                {"error": "GitHub App registration is already in progress"},
                status_code=409,
            )
        action = manifest_registration_url(
            correlation_state=registration.correlation_state,
            organization=registration.context.organization,
        )
        manifest = json.dumps(
            github_app_manifest(
                expected_origin,
                app_name=registration.app_name,
            ),
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

    @app.post("/setup/chatgpt/connect")
    async def connect_chatgpt() -> Response:
        started = await chatgpt_setup.start_authorization()
        if started is ChatGPTAuthorizationStart.FAILED:
            logger.warning(
                "ChatGPT authorization could not be started",
                extra={"event": "chatgpt_authorization_start_failed"},
            )
        return RedirectResponse("/setup", status_code=303)

    @app.post("/setup/chatgpt/cancel")
    async def cancel_chatgpt() -> Response:
        try:
            cancelled = await chatgpt_setup.cancel()
        except CodexOnboardingError:
            logger.warning(
                "ChatGPT authorization could not be cancelled",
                extra={"event": "chatgpt_authorization_cancel_failed"},
            )
            return JSONResponse(
                {"error": "ChatGPT authorization could not be cancelled; retry from setup"},
                status_code=502,
            )
        if not cancelled:
            return JSONResponse(
                {"error": "ChatGPT authorization is not pending"},
                status_code=409,
            )
        return RedirectResponse("/setup", status_code=303)

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
            possible_orphan = github_setup.retry_manifest_registration()
            logger.warning(
                "GitHub App registration failed",
                extra={"event": "github_manifest_exchange_failed"},
            )
            return JSONResponse(
                {
                    "error": (
                        "GitHub App registration outcome is uncertain. GitHub may have "
                        f'created an unused App with proposed name "{possible_orphan.name}"; '
                        "check GitHub Developer settings and delete it before or after "
                        "retrying from setup"
                    )
                },
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
        request.state.promote_reviewer = True
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
    warning = _possible_orphan_warning(view)
    if view.status == "connected":
        return f"{warning}Connected"
    if view.pending is not None:
        return (
            f"{warning}Awaiting installation. "
            f'<a href="{html.escape(installation_url(view.pending), quote=True)}">'
            "Continue on GitHub</a>"
        )
    organization = (
        view.context.organization.login
        if view.context is not None and view.context.organization is not None
        else ""
    )
    return (
        f"{warning}Not connected"
        '<form action="/setup/github/connect" method="post">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">'
        "<label>GitHub organization (optional) "
        f'<input name="organization" value="{html.escape(organization, quote=True)}" '
        'maxlength="39" '
        'pattern="[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?" '
        'autocomplete="off" autocapitalize="none" spellcheck="false"></label>'
        '<button type="submit">Connect GitHub</button>'
        "</form>"
    )


def _chatgpt_connection_html(view: ChatGPTSetupView, *, csrf_token: str) -> str:
    if view.status == "connected":
        return "Success. Connected"
    if isinstance(view, ChatGPTPendingView):
        return (
            "Pending. Open "
            f'<a href="{html.escape(view.authorization.verification_url, quote=True)}">'
            "the ChatGPT verification page</a> and enter code "
            f"<code>{html.escape(view.authorization.user_code)}</code>."
            '<form action="/setup/chatgpt/cancel" method="post">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">'
            '<button type="submit">Cancel ChatGPT</button>'
            "</form>"
        )
    button = "Retry ChatGPT" if view.status in {"failed", "cancelled"} else "Connect ChatGPT"
    return (
        f"{view.status.capitalize() if view.status != 'disconnected' else 'Not connected'}"
        '<form action="/setup/chatgpt/connect" method="post">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">'
        f'<button type="submit">{button}</button>'
        "</form>"
    )


def _possible_orphan_warning(view: GitHubSetupView) -> str:
    if view.context is None or not view.context.possible_orphan_apps:
        return ""
    apps = "".join(
        (
            "<li>Proposed name "
            f"<code>{html.escape(orphan.name)}</code> "
            f"({_github_app_owner_html(orphan.organization)})</li>"
        )
        for orphan in view.context.possible_orphan_apps
    )
    return (
        '<div role="alert">'
        "<p>A GitHub registration did not finish cleanly. GitHub may have created "
        "an unused App. Check GitHub Developer settings and delete any unused App "
        "listed below before or after retrying.</p>"
        f"<ul>{apps}</ul>"
        "</div>"
    )


def _github_app_owner_html(organization: GitHubOrganization | None) -> str:
    if organization is None:
        return "personal account"
    return f"organization <code>{html.escape(organization.login)}</code>"


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
    request.state.setup_form_values = values
    csrf_tokens = values.get("csrf_token", [])
    return csrf_tokens[0] if len(csrf_tokens) == 1 else None


def _github_organization(request: Request) -> GitHubOrganization | None:
    values: dict[str, list[str]] | None = getattr(request.state, "setup_form_values", None)
    if values is None or "organization" not in values:
        return None
    organizations = values["organization"]
    if len(organizations) != 1:
        raise ValueError("ambiguous GitHub organization")
    organization = organizations[0]
    if organization == "":
        return None
    return GitHubOrganization(organization)


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
    runtime = _StaticReviewerRuntime(
        active=ActiveReviewer(webhook_secret=webhook_secret, scheduler=scheduler)
    )
    app = _create_base_app(mode=lambda: runtime.mode, lifespan=lifespan)
    _register_github_webhook(app, runtime)
    return app


def _register_github_webhook(app: FastAPI, runtime: ReviewerRuntimePort) -> None:

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        active = runtime.active_reviewer()
        if active is None:
            return JSONResponse({"error": "reviewer unavailable"}, status_code=503)

        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type != "application/json":
            return JSONResponse({"error": "unsupported content type"}, status_code=415)

        body = await _read_bounded_body(request, max_bytes=MAX_WEBHOOK_BYTES)
        if body is None:
            return JSONResponse({"error": "webhook body too large"}, status_code=413)
        if not verify_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            active.webhook_secret,
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
        scheduled = await active.scheduler.schedule(review_request)
        status = "scheduled" if scheduled else "duplicate"
        return JSONResponse({"status": status}, status_code=202)


def _create_base_app(
    *,
    mode: Callable[[], RuntimeMode],
    lifespan: Lifespan[FastAPI] | None,
) -> FastAPI:
    app = FastAPI(title="Poppy", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": mode()}

    return app


@asynccontextmanager
async def _open_setup_access() -> AsyncIterator[bool]:
    yield True


class _UnavailableSetupRuntime:
    mode: RuntimeMode = "setup"
    setup_closed: bool = False

    def active_reviewer(self) -> None:
        return None

    def setup_access(self) -> AbstractAsyncContextManager[bool]:
        return _open_setup_access()

    async def promote_if_ready(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _StaticReviewerRuntime:
    active: ActiveReviewer
    mode: RuntimeMode = "reviewer"

    def active_reviewer(self) -> ActiveReviewer:
        return self.active


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
