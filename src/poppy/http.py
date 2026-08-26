from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from poppy.application.review_cycle import ReviewRequest
from poppy.webhook import InvalidWebhook, normalize_pull_request_webhook, verify_signature

MAX_WEBHOOK_BYTES = 1_000_000


class SchedulerPort(Protocol):
    async def schedule(self, request: ReviewRequest) -> bool: ...


def create_app(
    *,
    webhook_secret: bytes,
    scheduler: SchedulerPort,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    app = FastAPI(title="Poppy", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
