"""QQ 官方群信息接口（/v2/groups/{group_openid}/info 与 /bot_state）的单元测试。

两个接口都限 30 QPM，所以本文件重点覆盖省调用与不误调用：

- 成功结果按 group_openid + path 缓存，重复取不再打接口；bot_state 的 TTL 更短，
  因为它承载的是用户随时可改的推送开关。
- 失败也缓存（群名在入站时回填，否则每条消息都白打一次），但 TTL 短得多。
- 返回值与缓存隔离：调用方原地改动不污染后续读取。
- group_name 为 JSON null 时不能变成字面量 "None"。
- 频道消息的 group_id 存的是 channel_id，拿它打 /v2/groups 必然出错，因此必须
  只认原始消息上的 group_openid，频道与私聊场景直接返回 None、不发请求。
- 入站回填：群名进 message_obj.group（group_name_display 直接读它）；openid 缺失时
  退回「无群」，不能打到 /v2/groups/None/info 也不能让 session_id 变成 None。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import botpy
import botpy.message
import pytest
from botpy.connection import ConnectionSession

from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.platform.sources.qqofficial import qqofficial_message_event
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
    resolve_group,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    _ensure_group_message_create_parser,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    botClient as QQOfficialBotClient,
)

GROUP_INFO = {
    "group_openid": "group-1",
    "group_name": "试炼之地",
    "group_finger_memo": "",
    "group_class_text": "游戏",
    "group_tags": ["崩坏"],
    "group_member_num": 14,
}
BOT_STATE = {
    "member_openid": "member-1",
    "joined_at": "2026-07-15T20:12:02+08:00",
    "allow_proactive_msg": False,
    "recv_msg_setting": "",
    "member_role": "member",
}


@pytest.fixture(autouse=True)
def clear_cache():
    qqofficial_message_event._GROUP_QUERY_CACHE.clear()
    yield
    qqofficial_message_event._GROUP_QUERY_CACHE.clear()


def _make_event(raw, request: AsyncMock) -> QQOfficialMessageEvent:
    abm = AstrBotMessage()
    abm.message_id = "msg-1"
    abm.session_id = "group-1"
    abm.self_id = "bot-1"
    abm.sender = MessageMember(user_id="member-1", nickname="u")
    abm.type = MessageType.GROUP_MESSAGE
    abm.message_str = "ping"
    abm.message = []
    abm.raw_message = raw
    meta = PlatformMetadata(name="qq_official", description="t", id="qq_official")
    bot = SimpleNamespace(api=SimpleNamespace(_http=SimpleNamespace(request=request)))
    return QQOfficialMessageEvent(
        message_str="ping",
        message_obj=abm,
        platform_meta=meta,
        session_id="group-1",
        bot=bot,  # type: ignore[arg-type]
    )


def _group_event(request: AsyncMock) -> QQOfficialMessageEvent:
    raw = botpy.message.GroupMessage(
        api=None,
        event_id="event-1",
        data={
            "id": "msg-1",
            "author": {"member_openid": "member-1"},
            "group_openid": "group-1",
            "content": "ping",
            "timestamp": "0",
        },
    )
    return _make_event(raw, request)


def _guild_event(request: AsyncMock) -> QQOfficialMessageEvent:
    raw = botpy.message.Message(
        api=None,
        event_id="event-1",
        data={
            "id": "msg-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "author": {"id": "u1"},
            "content": "ping",
            "timestamp": "0",
        },
    )
    event = _make_event(raw, request)
    # 频道消息的 group_id 就是 channel_id，这正是不能直接拿来打 /v2/groups 的原因。
    event.message_obj.group_id = "channel-1"
    return event


def _c2c_event(request: AsyncMock) -> QQOfficialMessageEvent:
    raw = botpy.message.C2CMessage(
        api=None,
        event_id="event-1",
        data={
            "id": "msg-1",
            "author": {"user_openid": "user-1"},
            "content": "ping",
            "timestamp": "0",
        },
    )
    event = _make_event(raw, request)
    event.message_obj.type = MessageType.FRIEND_MESSAGE
    event.message_obj.group_id = ""
    return event


def _make_bot_client(request: AsyncMock):
    """构造一个可用于入站回填的 botpy Client 替身（client.api._http 构造时就存在）。"""
    client = QQOfficialBotClient(
        intents=botpy.Intents(public_messages=True),
        bot_log=False,
    )
    client.api._http.request = request  # type: ignore[method-assign]
    return client


def _make_group_message(group_openid) -> botpy.message.GroupMessage:
    _ensure_group_message_create_parser()
    dispatched: list = []
    connection = ConnectionSession(
        max_async=1,
        connect=lambda: None,
        dispatch=lambda event, message: dispatched.append(message),
        loop=asyncio.get_event_loop(),
        api=None,
    )
    connection.parser["group_message_create"](
        {
            "id": "event-1",
            "d": {
                "id": "msg-1",
                "content": "hello",
                "author": {"member_openid": "member-1"},
                "group_openid": group_openid,
                "mentions": [],
                "attachments": [],
            },
        }
    )
    return dispatched[0]


@pytest.mark.asyncio
async def test_resolve_group_skips_the_request_when_openid_is_missing():
    """openid 为空时不能打到 /v2/groups/None/info，也不能造出 group_id 为 None 的 Group。"""
    request = AsyncMock(return_value=GROUP_INFO)
    bot = _make_event(None, request).bot

    for missing in ("", None):
        assert await resolve_group(bot, missing) is None
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_openid", ["group-1", None])
async def test_inbound_group_message_fills_group_name(group_openid):
    """入站回填走的是适配器，缺 openid 时必须退回「无群」而不是 group_id=None。"""
    request = AsyncMock(return_value=GROUP_INFO)
    client = _make_bot_client(request)
    committed: list = []

    class PlatformStub:
        def remember_session_scene(self, session_id: str, scene: str) -> None:
            pass

        def remember_session_message_id(self, session_id: str, message_id: str) -> None:
            pass

        def create_event(self, message_obj):
            return message_obj

        def commit_event(self, event) -> None:
            committed.append(event)

    client.set_platform(cast(Any, PlatformStub()))
    await client.on_group_message_create(_make_group_message(group_openid))

    abm = committed[0]
    if group_openid is None:
        assert abm.group is None
        assert abm.group_id == ""
        assert abm.session_id == ""
        request.assert_not_awaited()
    else:
        assert abm.group is not None
        assert abm.group.group_name == "试炼之地"
        assert abm.group_id == "group-1"
        assert abm.session_id == "group-1"


@pytest.mark.asyncio
async def test_get_group_returns_name_and_caches_the_call():
    request = AsyncMock(return_value=GROUP_INFO)
    event = _group_event(request)

    group = await event.get_group()
    assert group is not None
    assert group.group_id == "group-1"
    assert group.group_name == "试炼之地"
    # QQ 官方没有群成员列表接口，也没有群主 / 管理员概念。
    assert group.members is None
    assert group.group_owner is None
    assert group.group_admins is None

    assert (await event.get_group()) is not None
    assert request.await_count == 1

    route = request.await_args_list[0].args[0]
    assert route.method == "GET"
    assert route.url.endswith("/v2/groups/group-1/info")


@pytest.mark.asyncio
async def test_group_info_and_bot_state_are_cached_separately():
    async def fake_request(route):
        return BOT_STATE if route.path.endswith("bot_state") else GROUP_INFO

    request = AsyncMock(side_effect=fake_request)
    event = _group_event(request)

    info = await event.get_group_info()
    assert info is not None and info["group_member_num"] == 14

    state = await event.get_group_bot_state()
    assert state is not None and state["member_role"] == "member"
    assert state["allow_proactive_msg"] is False

    # 两个 path 各自一次；再取一遍都走缓存。
    assert request.await_count == 2
    await event.get_group_info()
    await event.get_group_bot_state()
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_failure_falls_back_to_openid_and_is_cached_briefly(monkeypatch):
    """群名在入站时回填，失败若不缓存就会每条消息都白打一次，30 QPM 撑不住。"""
    request = AsyncMock(side_effect=RuntimeError("boom"))
    event = _group_event(request)

    group = await resolve_group(event.bot, "group-1")
    assert group.group_id == "group-1"
    assert group.group_name is None
    assert await event.get_group_info() is None
    assert request.await_count == 1

    # 负缓存过期后允许重试，偶发错误不会被钉住一整个 TTL。
    cached_at = next(iter(qqofficial_message_event._GROUP_QUERY_CACHE.values()))[0]
    monkeypatch.setattr(
        qqofficial_message_event.time,
        "time",
        lambda: (
            cached_at + qqofficial_message_event._GROUP_QUERY_NEGATIVE_TTL_SECONDS + 1
        ),
    )
    request.side_effect = None
    request.return_value = GROUP_INFO
    refreshed = await resolve_group(event.bot, "group-1")
    assert refreshed.group_name == "试炼之地"
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_get_group_reuses_the_inbound_group_object():
    """入站已回填过群名，当前会话不该再打一次接口。"""
    request = AsyncMock(return_value=GROUP_INFO)
    event = _group_event(request)
    event.message_obj.group = await resolve_group(event.bot, "group-1")
    request.reset_mock()

    group = await event.get_group()
    assert group is event.message_obj.group
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_guild_message_never_queries_the_group_api():
    request = AsyncMock(return_value=GROUP_INFO)
    event = _guild_event(request)

    assert await event.get_group() is None
    assert await event.get_group_info() is None
    assert await event.get_group_bot_state() is None
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_c2c_message_never_queries_the_group_api():
    request = AsyncMock(return_value=GROUP_INFO)
    event = _c2c_event(request)

    assert await event.get_group() is None
    assert await event.get_group_info() is None
    assert await event.get_group_bot_state() is None
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_group_id_overrides_the_current_chat():
    request = AsyncMock(return_value=GROUP_INFO)
    event = _guild_event(request)

    group = await event.get_group("group-2")
    assert group is not None and group.group_id == "group-2"
    route = request.await_args_list[0].args[0]
    assert route.url.endswith("/v2/groups/group-2/info")


@pytest.mark.asyncio
async def test_null_group_name_is_not_stringified():
    """group_name 为 JSON null 时不能变成字面量 "None"。"""
    request = AsyncMock(return_value={**GROUP_INFO, "group_name": None})
    event = _group_event(request)

    group = await event.get_group()
    assert group is not None
    assert group.group_name is None


@pytest.mark.asyncio
async def test_returned_payload_is_isolated_from_the_cache():
    """返回的是「原始字段」，调用方原地改动不能污染后续读取。"""
    request = AsyncMock(return_value=GROUP_INFO)
    event = _group_event(request)

    first = await event.get_group_info()
    assert first is not None
    first["group_name"] = "被改过的名字"
    first["group_tags"].append("污染")

    second = await event.get_group_info()
    assert second is not None
    assert second["group_name"] == "试炼之地"
    assert second["group_tags"] == ["崩坏"]
    # 群名同样来自这份缓存。
    group = await event.get_group()
    assert group is not None and group.group_name == "试炼之地"
    assert request.await_count == 1
    # 源响应对象本身也不该被写回。
    assert GROUP_INFO["group_tags"] == ["崩坏"]


@pytest.mark.asyncio
async def test_bot_state_expires_sooner_than_group_info(monkeypatch):
    """allow_proactive_msg 是随时可改的开关，脏读一小时会让推送前检查失去意义。"""

    async def fake_request(route):
        return BOT_STATE if route.path.endswith("bot_state") else GROUP_INFO

    request = AsyncMock(side_effect=fake_request)
    event = _group_event(request)
    await event.get_group_info()
    await event.get_group_bot_state()
    assert request.await_count == 2

    cached_at = next(iter(qqofficial_message_event._GROUP_QUERY_CACHE.values()))[0]
    bot_state_ttl = qqofficial_message_event._GROUP_QUERY_TTL_SECONDS[
        qqofficial_message_event._GROUP_BOT_STATE_PATH
    ]
    monkeypatch.setattr(
        qqofficial_message_event.time, "time", lambda: cached_at + bot_state_ttl + 1
    )

    # 群资料仍在有效期内，bot_state 已过期。
    await event.get_group_info()
    assert request.await_count == 2
    await event.get_group_bot_state()
    assert request.await_count == 3
