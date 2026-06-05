"""Discord 组件交互（按钮 / select / modal）相关单元测试。

覆盖未提交改动里的纯逻辑分支，使用真实 pycord（已安装），不依赖 discord mock fixture：

- components.py：pydantic 字段化组件的关键字构造、默认值隔离、View 不丢子类、to_discord_view 产物。
- client.py：modal 值扁平化、交互数据字典、on_interaction 路由（component/modal→回调，slash→Pycord）。
- discord_platform_adapter.py：交互→AstrBotMessage 转换与 convert_message 分发。
- discord_platform_event.py：QQ 兼容 shim 访问器（custom_id / values / modal_values / 类型判定）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from astrbot.api.platform import MessageType
from astrbot.core.platform.sources.discord.client import DiscordBotClient
from astrbot.core.platform.sources.discord.components import (
    DiscordButton,
    DiscordEmbed,
    DiscordReference,
    DiscordSelect,
    DiscordView,
)
from astrbot.core.platform.sources.discord.discord_platform_adapter import (
    DiscordPlatformAdapter,
)
from astrbot.core.platform.sources.discord.discord_platform_event import (
    DiscordPlatformEvent,
)


class FakeInteraction(discord.Interaction):
    """绕过 Interaction.__init__ 的轻量替身。

    discord.Interaction 的 __slots__ 已包含 id/type/data/user/channel/channel_id/guild_id，
    故可直接赋值；且 isinstance(obj, discord.Interaction) 成立，能通过被测代码里的类型判定。
    """

    def __init__(
        self,
        *,
        type=None,
        data=None,
        user=None,
        channel=None,
        guild_id=None,
        channel_id=None,
        id=999,
    ) -> None:
        self.type = type
        self.data = data
        self.user = user
        self.channel = channel
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.id = id


def _make_client() -> DiscordBotClient:
    """构造一个未经 Bot.__init__ 的 client，仅填测试需要的属性。

    ``Bot.user`` 是只读 property（读 ``_connection.user``），故注入 ``_connection`` 而非直接赋值。
    """
    client = object.__new__(DiscordBotClient)
    client._connection = SimpleNamespace(user=SimpleNamespace(id=123))
    return client


def _make_event(raw) -> DiscordPlatformEvent:
    """构造一个未经 __init__ 的事件，仅注入 message_obj.raw_message。"""
    ev = object.__new__(DiscordPlatformEvent)
    ev.message_obj = SimpleNamespace(raw_message=raw)
    return ev


# --------------------------------------------------------------------------- #
# components.py：pydantic 字段化组件
# --------------------------------------------------------------------------- #


def test_discord_button_keyword_construction():
    btn = DiscordButton(label="勾", custom_id="vote:yes")
    assert btn.type == "discord_button"
    assert btn.label == "勾"
    assert btn.custom_id == "vote:yes"
    # 默认值
    assert btn.style == "primary"
    assert btn.disabled is False
    assert btn.url is None
    assert btn.row is None


def test_discord_select_keyword_construction():
    sel = DiscordSelect(custom_id="pick", options=[{"label": "A"}], max_values=3)
    assert sel.type == "discord_select"
    assert sel.custom_id == "pick"
    assert sel.options == [{"label": "A"}]
    assert sel.select_type == "string"
    assert sel.min_values == 1
    assert sel.max_values == 3
    assert sel.disabled is False


def test_discord_reference_keyword_construction():
    ref = DiscordReference(message_id="11", channel_id="22")
    assert ref.type == "discord_reference"
    assert ref.message_id == "11"
    assert ref.channel_id == "22"


def test_discord_embed_mutable_default_isolated():
    """fields 默认 [] 必须按实例隔离，不能在实例间共享同一 list。"""
    a = DiscordEmbed(title="a")
    b = DiscordEmbed(title="b")
    a.fields.append({"name": "x", "value": "y"})
    assert a.fields == [{"name": "x", "value": "y"}]
    assert b.fields == []


def test_discord_view_preserves_component_subclasses():
    """View.components 用裸 list 注解，须保留 DiscordButton/DiscordSelect 子类而非降级为基类。"""
    btn = DiscordButton(label="x", custom_id="c")
    sel = DiscordSelect(custom_id="s", options=[{"label": "A"}])
    view = DiscordView(components=[btn, sel], timeout=60)
    assert view.timeout == 60
    assert len(view.components) == 2
    assert isinstance(view.components[0], DiscordButton)
    assert isinstance(view.components[1], DiscordSelect)
    assert view.components[0].custom_id == "c"


@pytest.mark.asyncio
async def test_to_discord_view_builds_buttons_and_select():
    # discord.ui.View 构造需在运行中的事件循环内（内部 asyncio.get_running_loop）。
    btn = DiscordButton(label="勾", custom_id="vote", style="success")
    url_btn = DiscordButton(label="link", url="https://example.com")
    sel = DiscordSelect(
        custom_id="pick",
        options=[{"label": "A", "value": "a"}, {"label": "B"}],
    )
    view = DiscordView(components=[btn, url_btn, sel]).to_discord_view()

    assert isinstance(view, discord.ui.View)
    buttons = [i for i in view.children if isinstance(i, discord.ui.Button)]
    selects = [i for i in view.children if isinstance(i, discord.ui.Select)]
    assert len(buttons) == 2
    assert len(selects) == 1

    callback_btn = next(b for b in buttons if b.custom_id == "vote")
    assert callback_btn.style == discord.ButtonStyle.success

    link_btn = next(b for b in buttons if b.url == "https://example.com")
    assert link_btn.style == discord.ButtonStyle.link

    # value 缺省回落到 label
    assert [o.value for o in selects[0].options] == ["a", "B"]


# --------------------------------------------------------------------------- #
# client.py：modal 值提取 / 交互数据 / on_interaction 路由
# --------------------------------------------------------------------------- #


def test_extract_modal_values_flattens_rows():
    data = {
        "components": [
            {"components": [{"custom_id": "name", "value": "Alice"}]},
            {"components": [{"custom_id": "age", "value": "30"}]},
        ]
    }
    inter = FakeInteraction(type=discord.InteractionType.modal_submit, data=data)
    assert DiscordBotClient._extract_modal_values(inter) == {
        "name": "Alice",
        "age": "30",
    }


def test_extract_modal_values_non_modal_returns_empty():
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "x"},
    )
    assert DiscordBotClient._extract_modal_values(inter) == {}


def test_create_interaction_data_component():
    client = _make_client()
    user = SimpleNamespace(id=456, display_name="Bob")
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "vote:yes", "values": ["a", "b"], "component_type": 2},
        user=user,
        channel_id=789,
        guild_id=111,
        id=222,
    )
    d = client._create_interaction_data(inter)
    assert d["type"] == "interaction"
    assert d["interaction"] is inter
    assert d["bot_id"] == "123"
    assert d["userid"] == "456"
    assert d["username"] == "Bob"
    assert d["channel_id"] == "789"
    assert d["guild_id"] == "111"
    assert d["message_id"] == "222"
    assert d["custom_id"] == "vote:yes"
    assert d["values"] == ["a", "b"]
    assert d["modal_values"] == {}


@pytest.mark.asyncio
async def test_on_interaction_routes_component_to_callback():
    client = _make_client()
    received = AsyncMock()
    client.on_interaction_received = received
    client.process_application_commands = AsyncMock()

    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "vote", "component_type": 2},
        user=SimpleNamespace(id=1, display_name="u"),
        channel_id=2,
        guild_id=3,
        id=4,
    )
    await client.on_interaction(inter)

    received.assert_awaited_once()
    client.process_application_commands.assert_not_awaited()
    assert received.await_args is not None
    assert received.await_args.args[0]["custom_id"] == "vote"


@pytest.mark.asyncio
async def test_on_interaction_routes_modal_to_callback():
    client = _make_client()
    received = AsyncMock()
    client.on_interaction_received = received
    client.process_application_commands = AsyncMock()

    data = {
        "custom_id": "signup",
        "components": [{"components": [{"custom_id": "name", "value": "Alice"}]}],
    }
    inter = FakeInteraction(
        type=discord.InteractionType.modal_submit,
        data=data,
        user=SimpleNamespace(id=1, display_name="u"),
        channel_id=2,
        guild_id=3,
        id=4,
    )
    await client.on_interaction(inter)

    received.assert_awaited_once()
    client.process_application_commands.assert_not_awaited()
    assert received.await_args is not None
    assert received.await_args.args[0]["modal_values"] == {"name": "Alice"}


@pytest.mark.asyncio
async def test_on_interaction_routes_slash_to_pycord():
    client = _make_client()
    client.on_interaction_received = AsyncMock()
    client.process_application_commands = AsyncMock()

    inter = FakeInteraction(
        type=discord.InteractionType.application_command,
        data={"name": "ping"},
    )
    await client.on_interaction(inter)

    client.process_application_commands.assert_awaited_once()
    client.on_interaction_received.assert_not_awaited()


# --------------------------------------------------------------------------- #
# discord_platform_adapter.py：交互 → AstrBotMessage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_convert_message_routes_interaction_group():
    adapter = object.__new__(DiscordPlatformAdapter)
    adapter.bot_self_id = "999"
    user = SimpleNamespace(id=456, display_name="Bob")
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "x"},
        user=user,
        channel=None,
        channel_id=789,
        guild_id=111,
        id=222,
    )
    data = {"type": "interaction", "interaction": inter, "bot_id": "999"}

    abm = await adapter.convert_message(data)

    assert abm.raw_message is inter
    # message_str/message 均留空（不参与路由）；日志概要由事件 get_message_outline 覆写生成
    assert abm.message_str == ""
    assert abm.message == []
    assert abm.sender.user_id == "456"
    assert abm.sender.nickname == "Bob"
    assert abm.self_id == "999"
    assert abm.session_id == "789"
    assert abm.message_id == "222"
    # channel 为 None 且有 guild_id → 群消息
    assert abm.type == MessageType.GROUP_MESSAGE
    assert abm.group_id == "789"


@pytest.mark.asyncio
async def test_convert_message_routes_interaction_dm():
    adapter = object.__new__(DiscordPlatformAdapter)
    adapter.bot_self_id = "999"
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "x"},
        user=SimpleNamespace(id=1, display_name="u"),
        channel=None,
        channel_id=789,
        guild_id=None,
        id=5,
    )
    data = {"type": "interaction", "interaction": inter}

    abm = await adapter.convert_message(data)

    # channel 为 None 且无 guild_id → 私聊
    assert abm.type == MessageType.FRIEND_MESSAGE


# --------------------------------------------------------------------------- #
# discord_platform_event.py：QQ 兼容 shim 访问器
# --------------------------------------------------------------------------- #


def test_event_button_accessors():
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "vote:yes", "values": ["a", "b"]},
    )
    ev = _make_event(inter)
    assert ev.is_button_interaction() is True
    assert ev.is_modal_submit() is False
    assert ev.get_interaction_custom_id() == "vote:yes"
    assert ev.get_interaction_values() == ["a", "b"]


def test_event_modal_accessors():
    data = {
        "custom_id": "signup",
        "components": [
            {"components": [{"custom_id": "name", "value": "Alice"}]},
            {"components": [{"custom_id": "age", "value": "30"}]},
        ],
    }
    inter = FakeInteraction(type=discord.InteractionType.modal_submit, data=data)
    ev = _make_event(inter)
    assert ev.is_modal_submit() is True
    assert ev.is_button_interaction() is False
    assert ev.get_modal_custom_id() == "signup"
    assert ev.get_modal_values() == {"name": "Alice", "age": "30"}


def test_event_accessors_non_interaction_safe():
    """raw_message 非 Interaction（如普通消息）时访问器须安全返回空，不抛异常。"""
    ev = _make_event(object())
    assert ev.is_button_interaction() is False
    assert ev.is_modal_submit() is False
    assert ev.get_interaction_values() == []
    assert ev.get_modal_values() == {}
    assert ev.get_modal_custom_id() == ""


def test_event_modal_values_empty_for_non_modal():
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "x"},
    )
    ev = _make_event(inter)
    assert ev.get_modal_values() == {}


def test_get_message_outline_button():
    inter = FakeInteraction(
        type=discord.InteractionType.component, data={"custom_id": "dc:confirm"}
    )
    assert _make_event(inter).get_message_outline() == "[按钮交互] dc:confirm"


def test_get_message_outline_select():
    inter = FakeInteraction(
        type=discord.InteractionType.component,
        data={"custom_id": "pick", "values": ["a", "b"]},
    )
    assert _make_event(inter).get_message_outline() == "[选择交互] pick = a, b"


def test_get_message_outline_modal():
    data = {
        "custom_id": "signup",
        "components": [{"components": [{"custom_id": "name", "value": "Alice"}]}],
    }
    inter = FakeInteraction(type=discord.InteractionType.modal_submit, data=data)
    out = _make_event(inter).get_message_outline()
    assert out == "[表单提交] signup = {'name': 'Alice'}"


def test_prefer_edit_origin_sets_flag():
    ev = _make_event(object())
    ev._prefer_edit_origin = False
    ev.prefer_edit_origin()
    assert ev._prefer_edit_origin is True
