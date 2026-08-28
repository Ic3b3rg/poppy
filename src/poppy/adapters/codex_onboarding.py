from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from openai_codex import AsyncCodex, AsyncDeviceCodeLoginHandle
from openai_codex.generated.v2_all import CancelLoginAccountStatus

from poppy.codex_runtime import CodexAuthenticationError, require_chatgpt_account

MAX_LOGIN_ID_CHARACTERS = 512
MAX_VERIFICATION_URL_CHARACTERS = 2_048
MAX_USER_CODE_CHARACTERS = 128


class CodexOnboardingError(RuntimeError):
    """A sanitized failure at the Codex App Server onboarding boundary."""


class CodexUnsupportedAccountError(CodexOnboardingError):
    """The completed login did not produce a managed ChatGPT account."""


@dataclass(frozen=True, slots=True)
class CodexDeviceAuthorization:
    login_id: str = field(repr=False)
    verification_url: str
    user_code: str

    def __post_init__(self) -> None:
        parsed_url = urlsplit(self.verification_url)
        if (
            not self.login_id
            or len(self.login_id) > MAX_LOGIN_ID_CHARACTERS
            or _contains_control_character(self.login_id)
            or len(self.verification_url) > MAX_VERIFICATION_URL_CHARACTERS
            or _contains_control_character(self.verification_url)
            or parsed_url.scheme != "https"
            or parsed_url.hostname != "auth.openai.com"
            or parsed_url.port is not None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or not self.user_code
            or len(self.user_code) > MAX_USER_CODE_CHARACTERS
            or any(character.isspace() for character in self.user_code)
            or _contains_control_character(self.user_code)
        ):
            raise ValueError("Codex device authorization data is invalid")


class CodexOnboardingPort(Protocol):
    async def start_device_authorization(self) -> CodexDeviceAuthorization: ...

    async def wait_for_completion(self, login_id: str) -> None: ...

    async def cancel(self, login_id: str) -> None: ...

    async def verify_chatgpt_account(self) -> None: ...


class CodexOnboarding:
    def __init__(self, *, client: AsyncCodex) -> None:
        self._client = client
        self._attempts: dict[str, AsyncDeviceCodeLoginHandle] = {}

    async def start_device_authorization(self) -> CodexDeviceAuthorization:
        handle: AsyncDeviceCodeLoginHandle | None = None
        try:
            handle = await self._client.login_chatgpt_device_code()
            authorization = CodexDeviceAuthorization(
                login_id=handle.login_id,
                verification_url=handle.verification_url,
                user_code=handle.user_code,
            )
        except Exception:
            if handle is not None:
                with suppress(Exception):
                    await handle.cancel()
            raise CodexOnboardingError("ChatGPT authorization could not be started") from None
        self._attempts[authorization.login_id] = handle
        return authorization

    async def wait_for_completion(self, login_id: str) -> None:
        handle = self._attempt(login_id)
        try:
            completed = await handle.wait()
            if not completed.success:
                raise CodexOnboardingError("ChatGPT authorization failed")
            await self.verify_chatgpt_account()
        except CodexOnboardingError:
            raise
        except Exception:
            raise CodexOnboardingError("ChatGPT authorization failed") from None
        finally:
            self._attempts.pop(login_id, None)

    async def verify_chatgpt_account(self) -> None:
        try:
            await require_chatgpt_account(self._client)
        except CodexAuthenticationError:
            with suppress(Exception):
                await self._client.logout()
            raise CodexUnsupportedAccountError(
                "ChatGPT authorization returned an unsupported account"
            ) from None
        except Exception:
            raise CodexOnboardingError("ChatGPT account could not be verified") from None

    async def cancel(self, login_id: str) -> None:
        handle = self._attempt(login_id)
        try:
            response = await handle.cancel()
            if response.status is not CancelLoginAccountStatus.canceled:
                raise CodexOnboardingError("ChatGPT authorization could not be cancelled")
        except CodexOnboardingError:
            raise
        except Exception:
            raise CodexOnboardingError("ChatGPT authorization could not be cancelled") from None
        finally:
            self._attempts.pop(login_id, None)

    def _attempt(self, login_id: str) -> AsyncDeviceCodeLoginHandle:
        try:
            return self._attempts[login_id]
        except KeyError:
            raise CodexOnboardingError("ChatGPT authorization is no longer active") from None


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
