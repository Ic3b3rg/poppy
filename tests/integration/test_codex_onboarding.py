from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import (
    Account,
    ApiKeyAccount,
    CancelLoginAccountResponse,
    CancelLoginAccountStatus,
    ChatgptAccount,
    GetAccountResponse,
    PlanType,
)

from poppy.adapters.codex_onboarding import (
    CodexOnboarding,
    CodexOnboardingError,
    CodexUnsupportedAccountError,
)


@pytest.mark.asyncio
async def test_device_authorization_uses_the_sdk_handle_and_validates_chatgpt_completion() -> None:
    handle = SimpleNamespace(
        login_id="login-1",
        verification_url="https://auth.openai.com/codex/device",
        user_code="ABCD-1234",
        wait=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
        cancel=AsyncMock(),
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    client.account = AsyncMock(return_value=chatgpt_account())
    onboarding = CodexOnboarding(client=cast(AsyncCodex, client))

    authorization = await onboarding.start_device_authorization()
    await onboarding.wait_for_completion(authorization.login_id)

    assert authorization.verification_url == "https://auth.openai.com/codex/device"
    assert authorization.user_code == "ABCD-1234"
    assert "login-1" not in repr(authorization)
    client.login_chatgpt_device_code.assert_awaited_once_with()
    handle.wait.assert_awaited_once_with()
    client.account.assert_awaited_once_with(refresh_token=True)


@pytest.mark.asyncio
async def test_failed_completion_is_sanitized_without_reading_an_account() -> None:
    provider_detail = "provider detail that must not escape"
    handle = SimpleNamespace(
        login_id="login-1",
        verification_url="https://auth.openai.com/codex/device",
        user_code="ABCD-1234",
        wait=AsyncMock(return_value=SimpleNamespace(success=False, error=provider_detail)),
        cancel=AsyncMock(),
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    client.account = AsyncMock()
    onboarding = CodexOnboarding(client=cast(AsyncCodex, client))
    authorization = await onboarding.start_device_authorization()

    with pytest.raises(CodexOnboardingError) as raised:
        await onboarding.wait_for_completion(authorization.login_id)

    assert provider_detail not in str(raised.value)
    client.account.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_key_account_is_logged_out_and_never_accepted_as_chatgpt() -> None:
    handle = SimpleNamespace(
        login_id="login-1",
        verification_url="https://auth.openai.com/codex/device",
        user_code="ABCD-1234",
        wait=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
        cancel=AsyncMock(),
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    client.account = AsyncMock(return_value=api_key_account())
    client.logout = AsyncMock()
    onboarding = CodexOnboarding(client=cast(AsyncCodex, client))
    authorization = await onboarding.start_device_authorization()

    with pytest.raises(CodexUnsupportedAccountError, match="unsupported account"):
        await onboarding.wait_for_completion(authorization.login_id)

    client.logout.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancel_uses_the_live_sdk_handle_and_rejects_a_stale_attempt() -> None:
    handle = SimpleNamespace(
        login_id="login-1",
        verification_url="https://auth.openai.com/codex/device",
        user_code="ABCD-1234",
        wait=AsyncMock(),
        cancel=AsyncMock(
            return_value=CancelLoginAccountResponse(status=CancelLoginAccountStatus.canceled)
        ),
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    onboarding = CodexOnboarding(client=cast(AsyncCodex, client))
    authorization = await onboarding.start_device_authorization()

    await onboarding.cancel(authorization.login_id)

    handle.cancel.assert_awaited_once_with()
    with pytest.raises(CodexOnboardingError, match="no longer active"):
        await onboarding.cancel(authorization.login_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verification_url", "user_code"),
    [
        ("https://attacker.example/device", "ABCD-1234"),
        (f"https://auth.openai.com/{'x' * 2_048}", "ABCD-1234"),
        ("https://auth.openai.com/codex/device", "x" * 129),
    ],
)
async def test_invalid_sdk_activation_data_is_rejected_without_echoing_it(
    verification_url: str,
    user_code: str,
) -> None:
    handle = SimpleNamespace(
        login_id="login-1",
        verification_url=verification_url,
        user_code=user_code,
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    onboarding = CodexOnboarding(client=cast(AsyncCodex, client))

    with pytest.raises(CodexOnboardingError) as raised:
        await onboarding.start_device_authorization()

    assert verification_url not in str(raised.value)
    assert user_code not in str(raised.value)


def chatgpt_account() -> GetAccountResponse:
    return GetAccountResponse(
        account=Account(root=ChatgptAccount(email=None, plan_type=PlanType.plus, type="chatgpt")),
        requires_openai_auth=False,
    )


def api_key_account() -> GetAccountResponse:
    return GetAccountResponse(
        account=Account(root=ApiKeyAccount(type="apiKey")),
        requires_openai_auth=False,
    )
