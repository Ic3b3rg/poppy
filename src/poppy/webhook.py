import hashlib
import hmac

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from poppy.application.review_cycle import PullRequestRef, ReviewRequest

SUPPORTED_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review", "edited"})
REVIEW_RELEVANT_EDITS = frozenset({"title", "body", "base"})


class InvalidWebhook(ValueError):
    """Raised when a signed webhook does not match the expected GitHub shape."""


class _Installation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)


class _Owner(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str = Field(min_length=1)


class _Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    owner: _Owner


class _Head(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=1)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ref: str = Field(min_length=1)


class _PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    body: str | None = None
    draft: bool
    head: _Head
    base: _Base


class _PullRequestWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    number: int = Field(gt=0)
    installation: _Installation
    repository: _Repository
    pull_request: _PullRequest
    changes: dict[str, object] = Field(default_factory=dict)


def verify_signature(body: bytes, signature: str | None, secret: bytes) -> bool:
    if signature is None or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def normalize_pull_request_webhook(
    body: bytes,
    *,
    delivery_id: str,
) -> ReviewRequest | None:
    try:
        payload = _PullRequestWebhook.model_validate_json(body)
    except ValidationError as error:
        raise InvalidWebhook("invalid pull_request webhook payload") from error

    if payload.action not in SUPPORTED_ACTIONS:
        return None
    if payload.action == "edited" and not REVIEW_RELEVANT_EDITS.intersection(payload.changes):
        return None

    return ReviewRequest(
        delivery_id=delivery_id,
        ref=PullRequestRef(
            installation_id=payload.installation.id,
            repository_id=payload.repository.id,
            owner=payload.repository.owner.login,
            repository=payload.repository.name,
            number=payload.number,
        ),
        head_sha=payload.pull_request.head.sha,
        title=payload.pull_request.title,
        body=payload.pull_request.body or "",
        base_ref=payload.pull_request.base.ref,
        is_draft=payload.pull_request.draft,
    )
