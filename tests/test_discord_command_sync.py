import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


class DiscordSyncError(Exception):
    """充当 discord.HTTPException 的替身：带 code，使配额判定 (code == 30034) 生效。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _fake_slash_command(**kwargs):
    cmd = MagicMock()
    cmd.name = kwargs.get("name")
    cmd.id = None
    cmd.guild_ids = kwargs.get("guild_ids")
    return cmd


def _build_adapter(monkeypatch: pytest.MonkeyPatch, extra_config: dict | None = None):
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

    config = {"discord_command_register": "force_startup"}
    if extra_config:
        config.update(extra_config)

    adapter = DiscordPlatformAdapter(config, {}, asyncio.Queue())
    client = MagicMock()
    client.sync_commands = AsyncMock()
    client._application_commands = {}
    client.command_mention_map = {}
    adapter.client = client
    return adapter, discord_platform_adapter


def _stub_schema_build(monkeypatch, adapter, mod, schemas: dict | None = None) -> None:
    monkeypatch.setattr(
        adapter,
        "_load_or_seed_command_schemas",
        lambda: (
            schemas
            or {"ping": {"enabled": True, "slash_name": "ping", "description": "ping"}}
        ),
    )
    monkeypatch.setattr(adapter, "_build_options", lambda _raw: [])
    monkeypatch.setattr(mod.discord, "SlashCommand", _fake_slash_command, raising=False)


@pytest.mark.asyncio
async def test_discord_command_sync_ignores_daily_quota(monkeypatch):
    adapter, mod = _build_adapter(monkeypatch)

    # 注册表非空才会走到 sync_commands()：空 schema 会在 _build_and_add_commands
    # 早退（"schema table is empty"），永远触达不到这里要测的配额错误分支。force_startup 模式
    # 跳过指纹短路（startup_if_changed 才比对指纹），直达 _sync_commands_guarded 的 sync。
    # SlashCommand 构造被替身（本测聚焦同步/配额处理，不验证 Pycord 指令对象内部）。
    _stub_schema_build(monkeypatch, adapter, mod)

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


@pytest.mark.asyncio
async def test_startup_if_changed_hydrates_id_cache_without_sync(monkeypatch):
    adapter, mod = _build_adapter(
        monkeypatch, {"discord_command_register": "startup_if_changed"}
    )
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(adapter, "_compute_command_fingerprint", lambda _cmds: "fp")
    monkeypatch.setattr(adapter, "_load_synced_fingerprint", lambda: "fp")
    monkeypatch.setattr(
        adapter, "_fetch_live_command_ids", AsyncMock(return_value={"ping": 111})
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_not_awaited()
    cache = adapter.client._application_commands
    assert cache["111"].name == "ping"
    assert cache["111"].id == 111
    assert cache[111] is cache["111"]
    assert adapter.client.command_mention_map["ping"] == "</ping:111>"


@pytest.mark.asyncio
async def test_startup_if_changed_resyncs_when_live_commands_missing(monkeypatch):
    adapter, mod = _build_adapter(
        monkeypatch, {"discord_command_register": "startup_if_changed"}
    )
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(adapter, "_compute_command_fingerprint", lambda _cmds: "fp")
    monkeypatch.setattr(adapter, "_load_synced_fingerprint", lambda: "fp")
    monkeypatch.setattr(adapter, "_fetch_live_command_ids", AsyncMock(return_value={}))
    warning = Mock()
    monkeypatch.setattr(mod.logger, "warning", warning)

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_awaited_once()
    warning.assert_called()
    assert any(
        "differ from local build" in str(call.args[0])
        for call in warning.call_args_list
    )


@pytest.mark.asyncio
async def test_startup_if_changed_ignores_live_orphan_commands(monkeypatch):
    adapter, mod = _build_adapter(
        monkeypatch, {"discord_command_register": "startup_if_changed"}
    )
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(adapter, "_compute_command_fingerprint", lambda _cmds: "fp")
    monkeypatch.setattr(adapter, "_load_synced_fingerprint", lambda: "fp")
    monkeypatch.setattr(
        adapter,
        "_fetch_live_command_ids",
        AsyncMock(return_value={"ping": 111, "orphan": 222}),
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_not_awaited()
    cache = adapter.client._application_commands
    assert cache["111"].name == "ping"
    assert "222" not in cache
    assert "orphan" not in adapter.client.command_mention_map


@pytest.mark.asyncio
async def test_startup_if_changed_uses_stored_ids_when_live_fetch_fails(monkeypatch):
    adapter, mod = _build_adapter(
        monkeypatch,
        {
            "discord_command_register": "startup_if_changed",
            "discord_command_ids": '{"ping": 333}',
        },
    )
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(adapter, "_compute_command_fingerprint", lambda _cmds: "fp")
    monkeypatch.setattr(adapter, "_load_synced_fingerprint", lambda: "fp")
    monkeypatch.setattr(
        adapter, "_fetch_live_command_ids", AsyncMock(return_value=None)
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_not_awaited()
    assert adapter.client._application_commands["333"].id == 333
    assert adapter.client._application_commands[333].id == 333


@pytest.mark.asyncio
async def test_startup_if_changed_resyncs_when_live_fetch_fails_without_ids(
    monkeypatch,
):
    adapter, mod = _build_adapter(
        monkeypatch, {"discord_command_register": "startup_if_changed"}
    )
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(adapter, "_compute_command_fingerprint", lambda _cmds: "fp")
    monkeypatch.setattr(adapter, "_load_synced_fingerprint", lambda: "fp")
    monkeypatch.setattr(
        adapter, "_fetch_live_command_ids", AsyncMock(return_value=None)
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_awaited_once()


def test_debug_guild_id_is_normalized_digit_string(monkeypatch):
    captured = []

    def _capture_slash_command(**kwargs):
        captured.append(kwargs)
        return _fake_slash_command(**kwargs)

    adapter, mod = _build_adapter(
        monkeypatch,
        {
            "discord_command_register": "force_startup",
            "discord_guild_id_for_debug": "123456789012345678",
        },
    )
    assert adapter.guild_id == "123456789012345678"
    _stub_schema_build(monkeypatch, adapter, mod)
    monkeypatch.setattr(
        mod.discord, "SlashCommand", _capture_slash_command, raising=False
    )

    built = adapter._build_and_add_commands()

    assert built is not None
    assert captured[0]["guild_ids"] == ["123456789012345678"]


def test_invalid_debug_guild_id_is_ignored(monkeypatch):
    adapter, _mod = _build_adapter(
        monkeypatch, {"discord_guild_id_for_debug": "not-a-snowflake"}
    )
    assert adapter.guild_id is None


@pytest.mark.asyncio
async def test_sync_failure_binds_stored_ids(monkeypatch):
    adapter, mod = _build_adapter(monkeypatch, {"discord_command_ids": '{"ping": 333}'})
    _stub_schema_build(monkeypatch, adapter, mod)
    adapter.client.sync_commands.side_effect = DiscordSyncError(
        "Max number of daily application command creates reached",
        code=30034,
    )

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_awaited_once()
    assert adapter.config["discord_command_ids"] == '{"ping": 333}'
    assert adapter.client._application_commands["333"].id == 333
    assert adapter.client.command_mention_map["ping"] == "</ping:333>"


@pytest.mark.asyncio
async def test_successful_sync_does_not_wipe_ids_when_pycord_omits_them(monkeypatch):
    adapter, mod = _build_adapter(monkeypatch, {"discord_command_ids": '{"ping": 444}'})
    _stub_schema_build(monkeypatch, adapter, mod)

    await adapter._sync_commands_by_mode()

    adapter.client.sync_commands.assert_awaited_once()
    assert adapter.config["discord_command_ids"] == '{"ping": 444}'
    assert adapter.client._application_commands["444"].id == 444
    assert adapter.client.command_mention_map["ping"] == "</ping:444>"


@pytest.mark.asyncio
async def test_successful_sync_merges_partial_backfill_into_stored_ids(monkeypatch):
    """Pycord 只回填部分指令时，未回填那条的存盘 id 不能被覆盖掉。"""
    adapter, mod = _build_adapter(
        monkeypatch, {"discord_command_ids": '{"ping": 111, "pong": 222}'}
    )
    _stub_schema_build(
        monkeypatch,
        adapter,
        mod,
        {
            "ping": {"enabled": True, "slash_name": "ping", "description": "ping"},
            "pong": {"enabled": True, "slash_name": "pong", "description": "pong"},
        },
    )

    async def _backfill_ping_only(*_args, **_kwargs):
        # Pycord 回填走 pending 匹配，miss 的指令 id 会留空；这里模拟只命中 ping。
        for call in adapter.client.add_application_command.call_args_list:
            command = call.args[0]
            if command.name == "ping":
                command.id = "999"

    adapter.client.sync_commands.side_effect = _backfill_ping_only

    await adapter._sync_commands_by_mode()

    assert json.loads(adapter.config["discord_command_ids"]) == {
        "ping": 999,
        "pong": 222,
    }
    assert adapter.client._application_commands["999"].name == "ping"


def test_pycord_guild_backfill_predicate_requires_string_guild_ids():
    """钉住我们依赖的 Pycord 契约：sync_commands 末尾 guild 回填比的是 HTTP 里的 str guild_id。

    谓词是照抄 Pycord 的（bot.py sync_commands 尾部的 find），所以本测钉住的是"适配器
    必须存 str guild_ids"这个依赖，而非 Pycord 的实现本身 —— Pycord 真改了这段，本测不会红。
    """
    import discord
    from discord.utils import find

    async def callback(ctx):
        return None

    payload = {
        "name": "ping",
        "type": 1,
        "guild_id": "123456789012345678",
        "id": "99",
    }

    def backfill_hit(cmd) -> bool:
        return (
            find(
                lambda c: (
                    c.name == payload["name"]
                    and c.type == payload.get("type")
                    and c.guild_ids is not None
                    and (guild_id := payload.get("guild_id"))
                    and guild_id in c.guild_ids
                ),
                [cmd],
            )
            is cmd
        )

    str_cmd = discord.SlashCommand(
        callback,
        name="ping",
        description="ping",
        guild_ids=["123456789012345678"],
    )
    int_cmd = discord.SlashCommand(
        callback,
        name="ping",
        description="ping",
        guild_ids=[123456789012345678],
    )
    assert backfill_hit(str_cmd) is True
    assert backfill_hit(int_cmd) is False


def test_pycord_name_fallback_matches_global_command_without_data_guild_id():
    """钉住我们依赖的 Pycord 契约：全局指令 interaction.data 无 guild_id，回退靠 None == None。

    同样是照抄谓词（bot.py process_application_commands 的 except KeyError 分支）。它说明
    全局作用域即使不 hydrate id 表也能路由 —— 这正是这个 bug 长期只在调试 guild 暴露的原因。
    """
    import discord

    async def callback(ctx):
        return None

    cmd = discord.SlashCommand(
        callback, name="ping", description="ping", guild_ids=None
    )
    data = {"name": "ping"}
    guild_id = data.get("guild_id")
    if guild_id:
        guild_id = int(guild_id)
    assert cmd.name == data["name"] and (
        guild_id == cmd.guild_ids
        or (isinstance(cmd.guild_ids, list) and guild_id in cmd.guild_ids)
    )
