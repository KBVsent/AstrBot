"""SNS client recording: serialization, inbound filtering, the writer and cleanup."""

import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import col, select, update

from astrbot.core.db.po import ImMessage, ImSession, UmoAlias, UmoSender
from astrbot.core.message.components import (
    At,
    ButtonInteraction,
    Image,
    Plain,
    Record,
    Reply,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.agent_wakeup_event import AgentWakeupEvent
from astrbot.core.platform.astr_message_event import AstrMessageEvent, event_context
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.sns_recorder import (
    SNS_UMO_EXTRA,
    SnsMessageWriter,
    SnsRetentionCleaner,
    get_sns_writer,
    preview_text,
    serialize_chain,
)
from astrbot.core.star.context import Context


class _Event(AstrMessageEvent):
    async def send(self, message):
        await super().send(message)


class _FakeDb:
    """Collects the batches the writer commits."""

    def __init__(self) -> None:
        self.writes = []

    async def insert_im_messages(self, writes):
        self.writes.extend(writes)
        return []


def _fake_db() -> Any:
    return _FakeDb()


def _event(
    chain,
    *,
    platform="aiocqhttp",
    sender="u1",
    group: bool = True,
) -> _Event:
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "g1" if group else sender
    message.message_id = "m1"
    message.sender = MessageMember(user_id=sender, nickname="Alice")
    message.message = chain
    message.message_str = ""
    if group:
        message.group = Group(group_id="g1", group_name="Group")
    return _Event(
        "",
        message,
        PlatformMetadata(name=platform, description="", id="p1"),
        message.session_id,
    )


def test_serialize_chain_keeps_names_and_classifies_images(tmp_path):
    temp_root = os.path.realpath(tmp_path)
    in_temp = os.path.join(temp_root, "a.jpg")
    chain = [
        Reply(id=42, sender_nickname="Bob", chain=[Plain("quoted"), Image(file="")]),
        At(qq="qq_official"),
        At(qq="u2", name="Carol"),
        Plain("look"),
        Image(file="https://cdn.example/x.png"),
        Image.fromFileSystem(in_temp),
        Image(file="/etc/hosts", url="/etc/hosts"),
        Image(file="base64://abcd"),
        Record(file="voice.wav"),
    ]

    content = serialize_chain(chain, "qq_official", temp_root)

    assert content == [
        {"type": "Reply", "id": "42", "sender_name": "Bob", "text": "quoted [Image]"},
        {"type": "At", "target": "u2", "name": "Carol"},
        {"type": "Plain", "text": "look"},
        {"type": "Image", "src": {"kind": "url", "value": "https://cdn.example/x.png"}},
        {"type": "Image", "src": {"kind": "file", "value": in_temp}},
        {"type": "Image", "src": {"kind": "none", "value": ""}},
        {"type": "Image", "src": {"kind": "none", "value": ""}},
        {"type": "Other", "name": "Record", "text": "[语音]"},
    ]
    # Only the QQ official adapter's own marker is dropped.
    assert serialize_chain([At(qq="qq_official")], "aiocqhttp", temp_root)
    assert (
        preview_text(content) == "@Carol look [Image] [Image] [Image] [Image] [Record]"
    )
    assert preview_text([{"type": "Plain", "text": "字\n" * 100}]) == "字 " * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "webchat",
        "wakeup",
        "callback",
        "empty",
        "self",
        "disabled",
    ],
)
async def test_inbound_filter_skips_non_user_messages(case):
    db = _fake_db()
    writer = SnsMessageWriter(db, {"sns_client": {"enable": case != "disabled"}})
    chain = [Plain("hi")]
    if case == "callback":
        chain = [ButtonInteraction(action_id="a", interaction_id="i")]
    event = _event(
        [] if case == "empty" else chain,
        platform="webchat" if case == "webchat" else "aiocqhttp",
        sender="bot" if case == "self" else "u1",
    )
    if case == "wakeup":
        event.event_kind = "agent_wakeup"

    writer.record_inbound(event)
    await writer.drain()

    assert db.writes == []


@pytest.mark.asyncio
async def test_inbound_records_once_with_original_umo():
    db = _fake_db()
    writer = SnsMessageWriter(db, {})
    event = _event([Plain("hi")])

    writer.record_inbound(event)
    # The empty-mention waiter re-queues a shallow copy after isolation and an At.
    event.session_id = "u1_g1"
    retry = copy.copy(event)
    retry.message_obj.message.insert(0, At(qq="bot"))
    writer.record_inbound(retry)
    await writer.drain()

    assert len(db.writes) == 1
    write = db.writes[0]
    assert write.message.umo == "p1:GroupMessage:g1"
    assert retry.get_extra(SNS_UMO_EXTRA) == "p1:GroupMessage:g1"
    assert write.message.content == [{"type": "Plain", "text": "hi"}]
    assert (write.session_name, write.message.sender_name) == ("Group", "Alice")


@pytest.mark.asyncio
async def test_quote_of_the_answered_event_is_completed_from_it():
    writer = SnsMessageWriter(_fake_db(), {})
    event = _event([Plain("sign in")])
    writer.record_inbound(event)
    umo = event.get_extra(SNS_UMO_EXTRA)
    chain = [Reply(id="m1"), Plain("ok")]

    writer.record_outbound(umo, "aiocqhttp", "bot", chain, event)
    event.is_callback_event = lambda: True
    writer.record_outbound(umo, "aiocqhttp", "bot", chain, event)

    writer._queue.get_nowait()
    assert writer._queue.get_nowait().message.content[0] == {
        "type": "Reply",
        "id": "m1",
        "sender_name": "Alice",
        "text": "sign in",
    }
    assert writer._queue.get_nowait().message.content == [
        {"type": "Plain", "text": "ok"}
    ]


@pytest.mark.asyncio
async def test_writer_commits_in_batches_and_counts_unread(temp_db, monkeypatch):
    batch_sizes = []
    insert = temp_db.insert_im_messages

    async def spy(writes):
        batch_sizes.append(len(writes))
        return await insert(writes)

    monkeypatch.setattr(temp_db, "insert_im_messages", spy)
    writer = SnsMessageWriter(temp_db, {})
    states = []
    writer.add_listener(
        lambda message, session: states.append((message.id, session.unread_count))
    )
    for index in range(55):
        writer.record_inbound(_event([Plain(f"in {index}")]))
    writer.record_outbound("p1:GroupMessage:g1", "aiocqhttp", "bot", [Plain("out")])
    await writer.drain()

    assert batch_sizes == [50, 6]
    assert [unread for _, unread in states] == [*range(1, 56), 55]
    async with temp_db.get_db() as session:
        im_session = (await session.execute(select(ImSession))).scalar_one()
        messages = (await session.execute(select(ImMessage))).scalars().all()
        alias = (await session.execute(select(UmoAlias))).scalar_one()
        sender = (await session.execute(select(UmoSender))).scalar_one()
    assert len(messages) == 56
    assert im_session.unread_count == 55
    assert im_session.last_message_id == states[-1][0]
    assert (im_session.last_preview, im_session.last_sender_name) == ("out", "")
    assert (alias.umo, alias.auto_name) == ("p1:GroupMessage:g1", "Group")
    assert (sender.sender_id, sender.sender_name) == ("u1", "Alice")


@pytest.mark.asyncio
async def test_failed_batch_is_retried_then_written_one_by_one(temp_db, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: real_sleep(0))
    writer = SnsMessageWriter(temp_db, {})
    for index in range(3):
        writer.record_inbound(_event([Plain(f"in {index}")]))
    queued = list(writer._queue._queue)  # type: ignore[attr-defined]
    queued[0].message.client_id = queued[1].message.client_id = "dup"
    await writer.drain()

    async with temp_db.get_db() as session:
        messages = (await session.execute(select(ImMessage))).scalars().all()
    assert sorted(m.content[0]["text"] for m in messages) == ["in 0", "in 2"]


@pytest.mark.asyncio
async def test_close_drains_queue_after_task_cancel(temp_db):
    writer = SnsMessageWriter(temp_db, {})
    writer.install()
    for index in range(3):
        writer.record_inbound(_event([Plain(f"in {index}")]))
    task = asyncio.create_task(writer.run())
    # Let the task take the first batch, then queue more behind it.
    await asyncio.sleep(0)
    for index in range(2):
        writer.record_inbound(_event([Plain(f"late {index}")]))
    task.cancel()
    await writer.close()

    assert task.cancelled()
    assert get_sns_writer() is None
    async with temp_db.get_db() as session:
        messages = (await session.execute(select(ImMessage))).scalars().all()
    assert len(messages) == 5


@pytest.mark.asyncio
async def test_outbound_send_paths_record_once():
    db = _fake_db()
    writer = SnsMessageWriter(db, {})
    inner = _event([], platform="discord", group=False)

    async def send_by_session(session, chain):
        # Discord, KOOK and WeCom deliver proactive sends through an event.
        await inner.send(chain)

    context: Any = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="p1", name="discord"),
                    send_by_session=send_by_session,
                )
            ]
        ),
        get_config=lambda **kwargs: {},
    )
    context.send_message = lambda session, chain: Context.send_message(
        context, session, chain
    )
    wakeup = AgentWakeupEvent(
        context=context,
        session=MessageSession.from_str("p1:FriendMessage:u1"),
        platform_meta=PlatformMetadata(id="p1", name="discord", description=""),
        prompt="scheduled",
        source="cron",
    )
    writer.install()
    try:
        await Context.send_message(
            context, "p1:FriendMessage:u1", MessageChain([Plain("direct")])
        )
        with event_context(wakeup):
            await wakeup.send(MessageChain([Plain("wakeup")]))
    finally:
        await writer.close()

    assert [write.message.content[0]["text"] for write in db.writes] == [
        "direct",
        "wakeup",
    ]
    assert {write.message.umo for write in db.writes} == {"p1:FriendMessage:u1"}


@pytest.mark.asyncio
async def test_cleanup_skips_kept_sessions_and_reruns_cleanly(temp_db):
    writer = SnsMessageWriter(temp_db, {})
    for sender in ("a", "b"):
        for index in range(3):
            writer.record_inbound(
                _event([Plain(str(index))], sender=sender, group=False)
            )
    await writer.drain()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    async with temp_db.get_db() as session:
        async with session.begin():
            await session.execute(
                update(ImMessage).where(col(ImMessage.id) <= 2).values(created_at=old)
            )
            await session.execute(
                update(ImMessage).where(col(ImMessage.id) >= 4).values(created_at=old)
            )
            await session.execute(
                update(ImSession)
                .where(col(ImSession.umo) == "p1:FriendMessage:b")
                .values(keep_forever=True)
            )
    cleaner = SnsRetentionCleaner(temp_db, {"sns_client": {"retention_days": 30}})

    assert await cleaner.cleanup_once() == 2
    assert await cleaner.cleanup_once() == 0

    async with temp_db.get_db() as session:
        remaining = (await session.execute(select(ImMessage.id))).scalars().all()
        sessions = {
            row.umo: row
            for row in (await session.execute(select(ImSession))).scalars().all()
        }
    assert sorted(remaining) == [3, 4, 5, 6]
    purged_until = sessions["p1:FriendMessage:a"].purged_until
    assert purged_until is not None and purged_until > old.replace(tzinfo=None)
    assert sessions["p1:FriendMessage:b"].purged_until is None
