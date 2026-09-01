import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

import httpx
import jwt
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
    private_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.app_id) is not int or self.app_id <= 0:
            raise ValueError("GitHub App id must be a positive integer")
        if re.fullmatch(r"[a-z0-9-]{1,100}", self.slug) is None:
            raise ValueError("GitHub App slug is invalid")
        if len(self.webhook_secret) < 16:
            raise ValueError("GitHub App webhook secret is invalid")
        _load_rsa_private_key(self.private_key)


@dataclass(frozen=True, slots=True)
class GitHubInstallation:
    installation_id: int
    account_id: int
    account_login: str
    target_type: str


class _ManifestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    pem: str = Field(min_length=1)
    webhook_secret: str = Field(min_length=16)

    @field_validator("pem")
    @classmethod
    def require_rsa_private_key(cls, value: str) -> str:
        _load_rsa_private_key(value)
        return value


class _InstallationAccount(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0)
    login: str = Field(min_length=1, max_length=100)
    type: str = Field(min_length=1, max_length=40)


class _InstallationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int = Field(gt=0)
    app_id: int = Field(gt=0)
    account: _InstallationAccount
    target_type: str = Field(min_length=1, max_length=40)
    repository_selection: Literal["all", "selected"]
    suspended_at: None = None


class GitHubOnboarding:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._clock = clock

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

    async def get_installation(
        self,
        *,
        app: GitHubAppCredentials,
        installation_id: int,
    ) -> GitHubInstallation:
        now = int(self._clock())
        try:
            app_jwt = jwt.encode(
                {
                    "iat": now - 60,
                    "exp": now + 9 * 60,
                    "iss": str(app.app_id),
                },
                app.private_key,
                algorithm="RS256",
            )
            response = await self._client.get(
                f"/app/installations/{installation_id}",
                headers=_github_headers(app_jwt),
            )
        except (httpx.HTTPError, jwt.PyJWTError) as error:
            raise GitHubOnboardingError("GitHub App installation is unavailable") from error
        _require_status(response, (200,))
        try:
            parsed = _InstallationResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise GitHubOnboardingError(
                "GitHub App installation returned an invalid response"
            ) from error
        if (
            parsed.id != installation_id
            or parsed.app_id != app.app_id
            or parsed.account.type != parsed.target_type
        ):
            raise GitHubOnboardingError("GitHub App installation does not belong to Poppy")
        return GitHubInstallation(
            installation_id=parsed.id,
            account_id=parsed.account.id,
            account_login=parsed.account.login,
            target_type=parsed.target_type,
        )


def _github_headers(app_jwt: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "application/vnd.github+json",
        "x-github-api-version": GITHUB_API_VERSION,
        "user-agent": "poppy-reviewer/0.1",
    }
    if app_jwt is not None:
        headers["authorization"] = f"Bearer {app_jwt}"
    return headers


def _require_status(response: httpx.Response, expected: tuple[int, ...]) -> None:
    if response.status_code in expected:
        return
    request_id = response.headers.get("x-github-request-id", "unknown")
    raise GitHubOnboardingError(
        f"GitHub API returned {response.status_code} (request_id={request_id})"
    )


def _load_rsa_private_key(value: str) -> rsa.RSAPrivateKey:
    try:
        private_key = serialization.load_pem_private_key(value.encode(), password=None)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("GitHub App private key is invalid") from error
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("GitHub App private key is not RSA")
    return private_key
