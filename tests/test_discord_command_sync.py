import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


class DiscordSyncError(Exception):
    """充当 discord.HTTPException 的替身：带 code，使配额判定 (code == 30034) 生效。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _build_adapter(monkeypatch: pytest.MonkeyPatch):
    """构造真实适配器（真实 discord，构造函数不联网），仅注入 mock client。

    用 DiscordSyncError 顶替 discord.HTTPException，让 `except discord.HTTPException`
    能捕获注入的同步错误（真实 HTTPException 需要 response 对象，构造麻烦且无必要）。
    """
    from astrbot.core.platform.sources.discord import discord_platform_adapter
    from astrbot.core.platform.sources.discord.discord_platform_adapter import (
        DiscordPlatformAdapter,
    )

    monkeypatch.setattr(discord_platform_adapter, "star_handlers_registry", [])
    monkeypatch.setattr(
        discord_platform_adapter.discord,
        "HTTPException",
        DiscordSyncError,
        raising=False,
    )

    adapter = DiscordPlatformAdapter(
        {"discord_command_register": "force_startup"},
        {},
        asyncio.Queue(),
    )
    client = MagicMock()
    client.sync_commands = AsyncMock()
    adapter.client = client
    return adapter, discord_platform_adapter


@pytest.mark.asyncio
async def test_discord_command_sync_ignores_daily_quota(monkeypatch):
    adapter, mod = _build_adapter(monkeypatch)

    # 注册表非空才会走到 sync_commands()：空 schema 会在 _build_and_add_commands
    # 早退（"schema table is empty"），永远触达不到这里要测的配额错误分支。force_startup 模式
    # 跳过指纹短路（startup_if_changed 才比对指纹），直达 _sync_commands_guarded 的 sync。
    # SlashCommand 构造被替身（本测聚焦同步/配额处理，不验证 Pycord 指令对象内部）。
    monkeypatch.setattr(
        adapter,
        "_load_or_seed_command_schemas",
        lambda: {"ping": {"enabled": True, "slash_name": "ping", "description": "ping"}},
    )
    monkeypatch.setattr(adapter, "_build_options", lambda _raw: [])

    # SlashCommand 替身需带真实 str name：_build_and_add_commands 会 ', '.join(c.name)。
    def _fake_slash_command(**kwargs):
        cmd = MagicMock()
        cmd.name = kwargs.get("name")
        cmd.id = None
        return cmd

    monkeypatch.setattr(
        mod.discord, "SlashCommand", _fake_slash_command, raising=False
    )

    warning = Mock()
    monkeypatch.setattr(mod.logger, "warning", warning)
    adapter.client.sync_commands.side_effect = DiscordSyncError(
        "Max number of daily application command creates reached",
        code=30034,
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_awaited_once()
    warning.assert_called_once()
    assert "30034" in warning.call_args.args[0]
