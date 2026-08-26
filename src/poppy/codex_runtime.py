from collections.abc import Callable
from pathlib import Path

from openai_codex import AsyncCodex, CodexConfig
from openai_codex.generated.v2_all import ChatgptAccount

_HARDENED_CONFIG = (
    'web_search="disabled"',
    "features.apps=false",
    "features.plugins=false",
    "features.multi_agent=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.enable_mcp_apps=false",
    "features.skill_search=false",
)


class CodexAuthenticationError(RuntimeError):
    """Poppy is not authenticated with a ChatGPT account."""


def build_codex_config(codex_home: Path) -> CodexConfig:
    return CodexConfig(
        config_overrides=_HARDENED_CONFIG,
        cwd=str(codex_home),
        env={
            "CODEX_HOME": str(codex_home),
            "OPENAI_API_KEY": "",
        },
        client_name="poppy",
        client_title="Poppy PR Reviewer",
    )


async def require_chatgpt_account(client: AsyncCodex) -> str:
    response = await client.account(refresh_token=True)
    if response.account is None or not isinstance(response.account.root, ChatgptAccount):
        raise CodexAuthenticationError(
            "Poppy requires a ChatGPT device login; API-key accounts are not accepted."
        )
    return response.account.root.plan_type.value


async def login_with_device_code(
    client: AsyncCodex,
    *,
    write: Callable[[str], None] = print,
) -> str:
    handle = await client.login_chatgpt_device_code()
    write(f"Open {handle.verification_url}")
    write(f"Enter code: {handle.user_code}")
    completed = await handle.wait()
    if not completed.success:
        raise CodexAuthenticationError("ChatGPT device login failed.")
    plan = await require_chatgpt_account(client)
    write(f"ChatGPT login complete (plan: {plan}).")
    return plan
