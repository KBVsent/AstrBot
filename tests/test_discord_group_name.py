"""Discord 频道名回填的单元测试。

频道名/服务器名取自 pycord 的本地缓存（MESSAGE_CREATE 载荷里只有 channel_id，对象
由 ConnectionState 从 GUILD_CREATE 填充的缓存解析），不产生任何 API 调用 —— 因此这里
没有缓存与失败兜底要测，重点是分流对不对：

- 服务器频道：group_name 填频道名（与 group_id 存的 channel_id 严格对应），
  服务器名另走事件 extras 的 guild_name。
- 没有 name 的 channel（私聊的 DMChannel、缓存未命中时的 PartialMessageable）不能
  把 group 清掉 —— group_id 在私聊下仍是 DM 频道 id，那是既有行为。
- group_owner 取服务器所有者，不能误取 Thread 自带的 owner_id（那是帖子创建者）。
- get_group()：当前会话复用入站对象；传别的 channel_id 走本地频道缓存，命中不了
  返回 None 且不打接口。
"""

from types import SimpleNamespace

import pytest

from astrbot.core.platform.sources.discord.discord_platform_adapter import (
    DiscordPlatformAdapter,
)
from astrbot.core.platform.sources.discord.discord_platform_event import (
    DiscordPlatformEvent,
)


def _make_adapter() -> DiscordPlatformAdapter:
    adapter = DiscordPlatformAdapter.__new__(DiscordPlatformAdapter)
    adapter.bot_self_id = "1"
    adapter.client = SimpleNamespace(user=SimpleNamespace(id=1))
    return adapter


def _make_guild(owner_id: int | None) -> SimpleNamespace:
    # get_member 是 role mention 清理路径要用的，与群名无关，返回 None 即可。
    return SimpleNamespace(
        id=777,
        name="我的服务器",
        owner_id=owner_id,
        get_member=lambda _user_id: None,
    )


def _make_message(channel, guild=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        content="hello",
        channel=channel,
        author=SimpleNamespace(id=2, display_name="tester"),
        attachments=[],
        guild=guild,
        role_mentions=[],
        reference=None,
    )


@pytest.mark.asyncio
async def test_guild_channel_message_fills_channel_name():
    guild = _make_guild(owner_id=999)
    channel = SimpleNamespace(id=123, name="general", guild=guild)
    abm = await _make_adapter().convert_message(
        {"message": _make_message(channel, guild)}
    )

    assert abm.group is not None
    assert abm.group.group_id == "123"
    assert abm.group.group_name == "general"
    assert abm.group.group_owner == "999"
    assert abm.group_id == "123"


@pytest.mark.asyncio
async def test_dm_channel_keeps_existing_group_id_without_a_name():
    """DMChannel 没有 name；不能因为取不到名字就把 group 清成 None。"""
    channel = SimpleNamespace(id=456, guild=None)
    abm = await _make_adapter().convert_message({"message": _make_message(channel)})

    assert abm.group_id == "456"
    assert abm.group is not None
    assert abm.group.group_name is None


@pytest.mark.asyncio
async def test_partial_messageable_is_treated_like_a_nameless_channel():
    """频道缓存未命中时 pycord 给的是 PartialMessageable：只有 id，没有 name。

    见 ConnectionState._get_guild_channel 末尾的 `channel or PartialMessageable(...)`。
    此时 message.guild 也是 None，回填必须整体跳过而不是崩掉。
    """
    channel = SimpleNamespace(id=789)  # 连 guild 属性都没有
    abm = await _make_adapter().convert_message({"message": _make_message(channel)})

    assert abm.group_id == "789"
    assert abm.group is not None
    assert abm.group.group_name is None


@pytest.mark.asyncio
async def test_thread_owner_id_is_not_mistaken_for_the_guild_owner():
    """Thread 自带 owner_id（帖子创建者），group_owner 必须取服务器所有者。"""
    guild = _make_guild(owner_id=999)
    channel = SimpleNamespace(id=321, name="讨论串", guild=guild, owner_id=111)
    abm = await _make_adapter().convert_message(
        {"message": _make_message(channel, guild)}
    )

    assert abm.group is not None
    assert abm.group.group_name == "讨论串"
    assert abm.group.group_owner == "999"


@pytest.mark.asyncio
async def test_thread_name_is_used_as_group_name():
    """帖子（Thread）同样有 name，不该被当成私聊。"""
    guild = _make_guild(owner_id=None)
    channel = SimpleNamespace(id=321, name="讨论串", guild=guild)
    abm = await _make_adapter().convert_message(
        {"message": _make_message(channel, guild)}
    )

    assert abm.group is not None
    assert abm.group.group_name == "讨论串"
    # owner_id 缺失时留空，而不是字面量 "None"。
    assert abm.group.group_owner is None


@pytest.mark.asyncio
async def test_guild_name_goes_to_extras():
    guild = _make_guild(owner_id=999)
    channel = SimpleNamespace(id=123, name="general", guild=guild)
    adapter = _make_adapter()
    adapter.config = {"id": "discord-test"}
    captured: list[DiscordPlatformEvent] = []
    original_create_event = adapter.create_event

    def capture(message, followup_webhook=None, is_ephemeral=False):
        event = original_create_event(message, followup_webhook, is_ephemeral)
        captured.append(event)
        return event

    adapter.create_event = capture  # type: ignore[method-assign]

    abm = await adapter.convert_message({"message": _make_message(channel, guild)})
    # raw_message 不是真正的 discord.Message，handle_msg 会在提及检测前退出，
    # 但 extras 在那之前就已写好 —— 这正是本用例要覆盖的部分。
    await adapter.handle_msg(abm)

    event = captured[0]
    assert event.get_extra("guild_name") == "我的服务器"
    assert event.get_extra("guild_id") == "777"
    # 频道名仍在 Group 里，两者不互相覆盖。
    assert event.message_obj.group is not None
    assert event.message_obj.group.group_name == "general"


@pytest.mark.asyncio
async def test_dm_does_not_write_guild_extras():
    channel = SimpleNamespace(id=456, guild=None)
    adapter = _make_adapter()
    adapter.config = {"id": "discord-test"}
    captured: list[DiscordPlatformEvent] = []
    original_create_event = adapter.create_event

    def capture(message, followup_webhook=None, is_ephemeral=False):
        event = original_create_event(message, followup_webhook, is_ephemeral)
        captured.append(event)
        return event

    adapter.create_event = capture  # type: ignore[method-assign]

    await adapter.handle_msg(
        await adapter.convert_message({"message": _make_message(channel)})
    )

    assert captured[0].get_extra("guild_name") is None
    assert captured[0].get_extra("guild_id") is None


@pytest.mark.asyncio
async def test_get_group_returns_none_in_private_chat():
    """基类约定私聊返回 None，不能把 group_id setter 造出的 Group 桩透出去。"""
    adapter = _make_adapter()
    adapter.config = {"id": "discord-test"}
    channel = SimpleNamespace(id=456, guild=None)
    abm = await adapter.convert_message({"message": _make_message(channel)})

    # 入站对象本身非空（group_id 在私聊下仍是 DM 频道 id），但 get_group 必须返回 None。
    assert abm.group is not None
    event = adapter.create_event(abm)
    assert await event.get_group() is None
    # 私聊里 get_group_id() 就是 DM 频道 id，显式传它与不传是同一种情况。
    assert await event.get_group(event.get_group_id()) is None


@pytest.mark.asyncio
async def test_get_group_reuses_inbound_object_and_resolves_other_channels():
    guild = _make_guild(owner_id=999)
    channel = SimpleNamespace(id=123, name="general", guild=guild)
    adapter = _make_adapter()
    adapter.config = {"id": "discord-test"}
    abm = await adapter.convert_message({"message": _make_message(channel, guild)})

    other = SimpleNamespace(id=456, name="announcements", guild=guild)
    lookups: list[int] = []

    def get_channel(channel_id: int):
        lookups.append(channel_id)
        return other if channel_id == 456 else None

    adapter.client.get_channel = get_channel
    event = adapter.create_event(abm)

    # 当前会话直接复用入站对象，不查频道缓存。
    current = await event.get_group()
    assert current is abm.group
    assert lookups == []

    resolved = await event.get_group("456")
    assert resolved is not None
    assert resolved.group_name == "announcements"
    assert lookups == [456]

    # 缓存里没有的频道（bot 不在该服务器）返回 None，不抛错。
    assert await event.get_group("999999") is None
    # 非法 id 同样不抛错。
    assert await event.get_group("not-an-id") is None
