from dataclasses import dataclass
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from poppy.adapters.github import GITHUB_API_VERSION


class GitHubOnboardingError(RuntimeError):
    """A sanitized failure at the GitHub onboarding boundary."""


@dataclass(frozen=True, slots=True)
class GitHubAppCredentials:
    app_id: int
    slug: str
    private_key: str
    webhook_secret: str


class _ManifestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    pem: str = Field(min_length=1)
    webhook_secret: str = Field(min_length=16)

    @field_validator("pem")
    @classmethod
    def require_rsa_private_key(cls, value: str) -> str:
        try:
            private_key = serialization.load_pem_private_key(value.encode(), password=None)
        except (TypeError, ValueError) as error:
            raise ValueError("GitHub App private key is invalid") from error
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("GitHub App private key is not RSA")
        return value


class GitHubOnboarding:
    def __init__(self, *, client: httpx.AsyncClient) -> None:
        self._client = client

    async def exchange_manifest(self, code: str) -> GitHubAppCredentials:
        try:
            response = await self._client.post(
                f"/app-manifests/{quote(code, safe='')}/conversions",
                headers=_github_headers(),
            )
        except httpx.HTTPError as error:
            raise GitHubOnboardingError("GitHub App registration is unavailable") from error
        _require_status(response, (201,))
        try:
            parsed = _ManifestResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise GitHubOnboardingError(
                "GitHub App registration returned an invalid response"
            ) from error
        return GitHubAppCredentials(
            app_id=parsed.id,
            slug=parsed.slug,
            private_key=parsed.pem,
            webhook_secret=parsed.webhook_secret,
        )


def _github_headers() -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "x-github-api-version": GITHUB_API_VERSION,
        "user-agent": "poppy-reviewer/0.1",
    }


def _require_status(response: httpx.Response, expected: tuple[int, ...]) -> None:
    if response.status_code in expected:
        return
    request_id = response.headers.get("x-github-request-id", "unknown")
    raise GitHubOnboardingError(
        f"GitHub API returned {response.status_code} (request_id={request_id})"
    )
