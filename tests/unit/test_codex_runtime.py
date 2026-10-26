from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import (
    Account,
    ChatgptAccount,
    GetAccountResponse,
    PlanType,
)

from poppy.codex_runtime import (
    CodexAuthenticationError,
    build_codex_config,
    login_with_device_code,
    require_chatgpt_account,
)


def test_codex_config_uses_private_home_and_disables_api_key_and_external_tools(
    tmp_path: Path,
) -> None:
    config = build_codex_config(tmp_path)

    assert config.env == {
        "CODEX_HOME": str(tmp_path),
        "OPENAI_API_KEY": "",
    }
    assert 'cli_auth_credentials_store="file"' in config.config_overrides
    assert 'forced_login_method="chatgpt"' in config.config_overrides
    assert 'web_search="disabled"' in config.config_overrides
    assert "features.apps=false" in config.config_overrides
    assert "features.plugins=false" in config.config_overrides
    assert "features.multi_agent=false" in config.config_overrides
    assert "features.browser_use=false" in config.config_overrides
    assert "features.computer_use=false" in config.config_overrides


@pytest.mark.asyncio
async def test_device_login_prints_only_activation_data_and_requires_chatgpt() -> None:
    handle = SimpleNamespace(
        verification_url="https://auth.openai.com/device",
        user_code="ABCD-EFGH",
        wait=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
    )
    client = MagicMock(spec=AsyncCodex)
    client.login_chatgpt_device_code = AsyncMock(return_value=handle)
    client.account = AsyncMock(return_value=chatgpt_account())
    messages: list[str] = []

    plan = await login_with_device_code(cast(AsyncCodex, client), write=messages.append)

    assert plan == "plus"
    assert messages == [
        "Open https://auth.openai.com/device",
        "Enter code: ABCD-EFGH",
        "ChatGPT login complete (plan: plus).",
    ]
    client.account.assert_awaited_once_with(refresh_token=True)


@pytest.mark.asyncio
async def test_non_chatgpt_account_is_rejected() -> None:
    client = MagicMock(spec=AsyncCodex)
    client.account = AsyncMock(
        return_value=GetAccountResponse(account=None, requires_openai_auth=True)
    )

    with pytest.raises(CodexAuthenticationError, match="ChatGPT"):
        await require_chatgpt_account(cast(AsyncCodex, client))


def chatgpt_account() -> GetAccountResponse:
    return GetAccountResponse(
        account=Account(root=ChatgptAccount(email=None, plan_type=PlanType.plus, type="chatgpt")),
        requires_openai_auth=False,
    )
