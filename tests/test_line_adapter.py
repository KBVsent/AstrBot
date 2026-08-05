"""LINE 适配器单元测试。

覆盖 LINE 的平台硬约束与容易回归的分支：

- webhook 受理：签名 / 关闭 / 队列满，以及去重登记必须发生在成功入队之后（否则 503
  之后 LINE 重投会被当成重复事件永久丢弃）。
- 发送结果：只有 2xx 算成功；400 只在 reply 且明确指向 reply token 时才归类为 token 失效。
- 引用：quoteToken 按聊天作用域隔离；bot 自己发出的文本被引用时能从本地缓存恢复。
- 入站：@全体映射为标准 AtAll；mention 偏移按 UTF-16 计量。
- Postback：走 raw_message（无消息组件），且显式禁止默认 LLM。
- 媒体：外链音频依赖 Record.duration；图床 URL 不被适配器校验，而自己拼的兜底 URL 要查
  HTTPS；MediaResolver(max_bytes=...) 能中止超限下载。
- Flex / 原始消息对象原样透传；Flex 里的媒体占位符按 profile 收敛后替换成公网 URL，
  拿不到 URL 就整条降级为 alt_text，而 LineRawMessage 永不做媒体解析。
"""

import asyncio
import base64
import hmac
import json
import time
from hashlib import sha256
from pathlib import Path

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, AtAll, Plain, Record, Reply
from astrbot.core.message.components import Image
from astrbot.core.platform.sources.line import line_media
from astrbot.core.platform.sources.line.components import (
    LineFlex,
    LineFlexMedia,
    LinePostbackAction,
    LineQuickReply,
    LineQuickReplyItem,
    LineRawMessage,
)
from astrbot.core.platform.sources.line.line_adapter import LinePlatformAdapter
from astrbot.core.platform.sources.line.line_api import (
    LineAPIClient,
    LineErrorCategory,
    LineSendResult,
    LineSentMessage,
)
from astrbot.core.platform.sources.line.line_cache import LineQuoteStore
from astrbot.core.platform.sources.line.line_event import (
    build_line_batch,
    finalize_line_messages,
    remember_sent_messages,
)
from astrbot.core.utils.io import MediaTooLargeError

CHANNEL_SECRET = "test-secret"


class FakeRequest:
    """最小 Quart 请求替身：带正确签名的 webhook 请求。"""

    def __init__(self, payload: dict, *, valid_signature: bool = True) -> None:
        self._payload = payload
        self._raw = json.dumps(payload).encode()
        signature = base64.b64encode(
            hmac.new(CHANNEL_SECRET.encode(), self._raw, sha256).digest()
        ).decode()
        self.headers = {"x-line-signature": signature if valid_signature else "invalid"}

    async def get_data(self) -> bytes:
        return self._raw

    async def get_json(self, silent: bool = False) -> dict:  # noqa: ARG002
        return self._payload


def make_adapter() -> LinePlatformAdapter:
    committed: asyncio.Queue = asyncio.Queue()
    adapter = LinePlatformAdapter(
        {
            "id": "line",
            "channel_access_token": "token",
            "channel_secret": CHANNEL_SECRET,
        },
        {},
        committed,
    )
    adapter.committed_events = committed

    async def fake_display_name(*_args) -> str:
        return "Display Name"

    adapter.line_api.get_user_display_name = fake_display_name  # type: ignore[method-assign]
    adapter.line_api.get_group_member_display_name = fake_display_name  # type: ignore[method-assign]
    adapter.line_api.get_room_member_display_name = fake_display_name  # type: ignore[method-assign]
    return adapter


def text_event(event_id: str, text: str = "hi", **message_extra) -> dict:
    return {
        "type": "message",
        "webhookEventId": event_id,
        "replyToken": "reply-token",
        "timestamp": 1,
        "source": {"type": "user", "userId": "U1"},
        "message": {
            "id": f"m-{event_id}",
            "type": "text",
            "text": text,
            **message_extra,
        },
    }


# ------------------------------------------------------------------ webhook


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature_and_shutdown():
    adapter = make_adapter()

    _, status = await adapter.webhook_callback(
        FakeRequest({"events": []}, valid_signature=False)
    )
    assert status == 400

    adapter.shutdown_event.set()
    _, status = await adapter.webhook_callback(FakeRequest({"events": []}))
    assert status == 503


@pytest.mark.asyncio
async def test_rejected_event_is_not_deduped_and_survives_redelivery():
    """队列满时返回 503 的事件不能被登记去重，否则重投会被当成重复而永久丢失。"""
    adapter = make_adapter()
    adapter._inbound_queue = asyncio.Queue(maxsize=1)
    adapter._inbound_queue.put_nowait({"placeholder": True})

    payload = {"events": [text_event("e1")]}
    _, status = await adapter.webhook_callback(FakeRequest(payload))
    assert status == 503

    # 腾出空位后重投：事件必须被受理。
    adapter._inbound_queue.get_nowait()
    adapter._inbound_queue.task_done()
    _, status = await adapter.webhook_callback(FakeRequest(payload))
    assert status == 200
    assert adapter._inbound_queue.qsize() == 1


@pytest.mark.asyncio
async def test_partially_accepted_payload_dedupes_only_enqueued_events():
    """部分受理时：已入队的重投被跳过，被拒的那条重投时补做。"""
    adapter = make_adapter()
    adapter._inbound_queue = asyncio.Queue(maxsize=1)

    payload = {"events": [text_event("e1"), text_event("e2")]}
    _, status = await adapter.webhook_callback(FakeRequest(payload))
    assert status == 503
    assert adapter._inbound_queue.qsize() == 1
    first = adapter._inbound_queue.get_nowait()
    adapter._inbound_queue.task_done()
    assert first["webhookEventId"] == "e1"

    _, status = await adapter.webhook_callback(FakeRequest(payload))
    assert status == 200
    assert adapter._inbound_queue.qsize() == 1
    second = adapter._inbound_queue.get_nowait()
    assert second["webhookEventId"] == "e2"


@pytest.mark.asyncio
async def test_duplicate_event_is_skipped_once_accepted():
    adapter = make_adapter()
    payload = {"events": [text_event("e1")]}

    await adapter.webhook_callback(FakeRequest(payload))
    await adapter.webhook_callback(FakeRequest(payload))
    assert adapter._inbound_queue.qsize() == 1


@pytest.mark.asyncio
async def test_terminate_counts_in_flight_events():
    """关闭超时的「未处理」条数要包含 worker 已取出但没做完的事件。"""
    adapter = make_adapter()
    started = asyncio.Event()
    warnings: list[tuple] = []

    async def slow_process(_event):
        started.set()
        await asyncio.sleep(5)

    adapter._process_event = slow_process  # type: ignore[method-assign]
    import astrbot.core.platform.sources.line.line_adapter as adapter_module

    original_warning = adapter_module.logger.warning
    original_timeout = adapter_module._TERMINATE_TIMEOUT_SECONDS
    adapter_module.logger.warning = lambda *args: warnings.append(args)
    adapter_module._TERMINATE_TIMEOUT_SECONDS = 0.1

    run_task = asyncio.create_task(adapter.run())
    try:
        adapter._inbound_queue.put_nowait({"a": 1})
        adapter._inbound_queue.put_nowait({"b": 2})
        await asyncio.wait_for(started.wait(), timeout=1)
        await adapter.terminate()
    finally:
        adapter_module.logger.warning = original_warning
        adapter_module._TERMINATE_TIMEOUT_SECONDS = original_timeout
        await run_task

    timeout_logs = [w for w in warnings if "terminate timed out" in str(w[0])]
    assert timeout_logs, warnings
    # 2 条事件：1 条在飞 + 1 条排队（或都在飞），总数必须 > 0。
    assert timeout_logs[0][1] > 0
    assert adapter.line_api._closed


# --------------------------------------------------------------- 发送结果


@pytest.mark.asyncio
async def test_non_2xx_status_is_failure(monkeypatch):
    client = LineAPIClient(channel_access_token="t", channel_secret="s")

    class FakeResponse:
        status = 302

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class FakeSession:
        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(client, "_get_session", lambda: _async_value(FakeSession()))
    result = await client.reply_message("token", [{"type": "text", "text": "x"}])
    assert result.ok is False
    assert result.status == 302
    assert result.error_category is LineErrorCategory.HTTP_ERROR


def _async_value(value):
    async def _wrapper():
        return value

    return _wrapper()


def test_error_classification_scopes_reply_token():
    classify = LineAPIClient._classify_error

    assert (
        classify(400, "Invalid reply token", None, "reply")
        is LineErrorCategory.REPLY_TOKEN_INVALID
    )
    # push 没有 reply token；quoteToken 之类的错误也不能被误判。
    assert classify(400, "Invalid reply token", None, "push") is (
        LineErrorCategory.BAD_REQUEST
    )
    assert (
        classify(400, "Invalid quoteToken", None, "reply")
        is LineErrorCategory.BAD_REQUEST
    )
    assert classify(401, "", None, "reply") is LineErrorCategory.UNAUTHORIZED
    assert classify(429, "", None, "reply") is LineErrorCategory.RATE_LIMITED
    assert classify(503, "", None, "reply") is LineErrorCategory.SERVER_ERROR
    assert classify(418, "", None, "reply") is LineErrorCategory.HTTP_ERROR


def test_non_json_error_body_is_kept_truncated():
    client = LineAPIClient(channel_access_token="t", channel_secret="s")
    assert client._parse_json_body("<html>502 Bad Gateway</html>") is None


# ------------------------------------------------------------------- 引用


@pytest.mark.asyncio
async def test_quote_token_is_chat_scoped():
    store = LineQuoteStore()
    store.put_token("C1", "m1", "token-1")

    batch = await build_line_batch(MessageChain(chain=[Reply(id="m1"), Plain("hi")]))
    assert batch.quote_message_id == "m1"

    same_chat = finalize_line_messages(batch, quote_store=store, chat_id="C1")
    assert same_chat[0]["quoteToken"] == "token-1"

    other_chat = finalize_line_messages(batch, quote_store=store, chat_id="C2")
    assert "quoteToken" not in other_chat[0]


def test_outbound_text_is_cached_for_later_quoting():
    """用户引用 bot 发出的文本时，LINE 无法回查文本，本地缓存是唯一恢复来源。"""
    store = LineQuoteStore()
    payload = [
        {"type": "text", "text": "bot says hi"},
        {"type": "image", "originalContentUrl": "https://x/a.jpg"},
    ]
    result = LineSendResult(
        ok=True,
        status=200,
        sent_messages=[
            LineSentMessage(id="s1", quote_token="qt-1"),
            LineSentMessage(id="s2", quote_token="qt-2"),
        ],
    )
    remember_sent_messages(payload, result, quote_store=store, chat_id="C1")

    assert store.get_token("C1", "s1") == "qt-1"
    cached = store.get_content("C1", "s1")
    assert cached and isinstance(cached[0], Plain) and cached[0].text == "bot says hi"
    # 媒体消息不缓存内容：外链可能早已失效，存下来没有意义。
    assert store.get_content("C1", "s2") is None


@pytest.mark.asyncio
async def test_inbound_reply_recovers_cached_content():
    adapter = make_adapter()
    first = await adapter.convert_message(text_event("e1", "original text"))
    assert first is not None

    quoting = text_event("e2", "quoting")
    quoting["message"]["quotedMessageId"] = first.message_id
    second = await adapter.convert_message(quoting)
    assert second is not None
    reply = second.message[0]
    assert isinstance(reply, Reply)
    assert reply.id == first.message_id
    assert reply.message_str == "original text"


# ------------------------------------------------------------------- 入站


@pytest.mark.asyncio
async def test_inbound_mention_all_maps_to_at_all():
    adapter = make_adapter()
    event = text_event("e1", "@all hello")
    event["source"] = {"type": "group", "groupId": "G1", "userId": "U1"}
    event["message"]["mention"] = {
        "mentionees": [{"index": 0, "length": 4, "type": "all"}]
    }

    abm = await adapter.convert_message(event)
    assert abm is not None
    assert isinstance(abm.message[0], AtAll)


@pytest.mark.asyncio
async def test_inbound_mention_offsets_use_utf16():
    adapter = make_adapter()
    event = text_event("e1", "😀@bob hi")
    event["message"]["mention"] = {
        "mentionees": [{"index": 2, "length": 4, "type": "user", "userId": "U9"}]
    }

    abm = await adapter.convert_message(event)
    assert abm is not None
    assert isinstance(abm.message[0], Plain) and abm.message[0].text == "😀"
    mention = abm.message[1]
    assert isinstance(mention, At) and mention.name == "bob"


# ---------------------------------------------------------------- postback


@pytest.mark.asyncio
async def test_postback_uses_raw_message_and_blocks_default_llm():
    adapter = make_adapter()
    event = {
        "type": "postback",
        "webhookEventId": "p1",
        "replyToken": "reply-token",
        "timestamp": 1,
        "source": {"type": "user", "userId": "U1"},
        "postback": {"data": "action=buy&id=1", "params": {"date": "2026-08-03"}},
    }

    abm = await adapter.convert_message(event)
    assert abm is not None
    assert abm.message == []
    assert abm.message_str == ""

    await adapter.handle_msg(abm)
    astr_event = adapter.committed_events.get_nowait()
    assert astr_event.is_postback() is True
    assert astr_event.get_postback_data() == "action=buy&id=1"
    assert astr_event.get_postback_params() == {"date": "2026-08-03"}
    # 未被插件处理的 postback 不得触发默认 LLM。
    assert astr_event.call_llm is True


@pytest.mark.asyncio
async def test_message_event_is_not_postback():
    adapter = make_adapter()
    abm = await adapter.convert_message(text_event("e1"))
    assert abm is not None
    await adapter.handle_msg(abm)
    astr_event = adapter.committed_events.get_nowait()
    assert astr_event.is_postback() is False
    assert astr_event.get_postback_data() is None
    assert astr_event.call_llm is False


# ------------------------------------------------------------------- 媒体


@pytest.mark.asyncio
async def test_external_audio_requires_duration():
    without_duration = await build_line_batch(
        MessageChain(chain=[Record(file="https://example.com/a.m4a")])
    )
    assert without_duration.messages == []

    with_duration = await build_line_batch(
        MessageChain(chain=[Record(file="https://example.com/a.m4a", duration=3210)])
    )
    assert with_duration.messages == [
        {
            "type": "audio",
            "originalContentUrl": "https://example.com/a.m4a",
            "duration": 3210,
        }
    ]


@pytest.mark.asyncio
async def test_image_host_url_is_trusted_but_fallback_url_is_checked(
    tmp_path, monkeypatch
):
    media_path = tmp_path / "a.jpg"
    media_path.write_bytes(b"\xff\xd8\xff\xd9")

    # 图床返回什么就用什么：图床由使用者配置，适配器不检查、不探测。
    async def fake_upload(*_args, **_kwargs):
        return "http://cdn.example.com/a.jpg"

    monkeypatch.setattr(line_media, "_upload_to_image_host", fake_upload)
    assert (
        await line_media.resolve_public_media_url(str(media_path), None)
        == "http://cdn.example.com/a.jpg"
    )

    # 自己拼出来的兜底 URL 必须是 HTTPS，否则该媒体被跳过。
    async def no_upload(*_args, **_kwargs):
        return None

    monkeypatch.setattr(line_media, "_upload_to_image_host", no_upload)

    async def http_fallback(_path):
        return "http://localhost:6185/api/file/token"

    monkeypatch.setattr(line_media, "_register_reusable_temp_url", http_fallback)
    assert await line_media.resolve_public_media_url(str(media_path), None) is None

    async def https_fallback(_path):
        return "https://bot.example.com/api/file/token"

    monkeypatch.setattr(line_media, "_register_reusable_temp_url", https_fallback)
    assert (
        await line_media.resolve_public_media_url(str(media_path), None)
        == "https://bot.example.com/api/file/token"
    )


@pytest.mark.asyncio
async def test_media_resolver_max_bytes_aborts_download(tmp_path, monkeypatch):
    """缺 Content-Length 也不能超限：累计字节到上限即中止。"""
    from astrbot.core.utils import media_utils

    monkeypatch.setattr(media_utils, "get_astrbot_temp_path", lambda: str(tmp_path))

    async def oversize_download(_url, target_path, *, max_bytes=None):
        from astrbot.core.utils.io import MediaTooLargeError as TooLarge

        written = 0
        with open(target_path, "wb") as f:
            for _ in range(10):
                chunk = b"x" * 1024
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise TooLarge(f"size exceeds limit {max_bytes}")
                f.write(chunk)

    monkeypatch.setattr(media_utils, "download_file", oversize_download)

    with pytest.raises(MediaTooLargeError):
        await media_utils.MediaResolver(
            "https://example.com/big.bin", max_bytes=2048
        ).to_path()


@pytest.mark.asyncio
async def test_external_image_download_failure_skips_component(monkeypatch):
    """外链图片抓不动就跳过，绝不退回直用外链（可能是 GIF/WebP，会让整批 400）。"""
    import astrbot.core.platform.sources.line.line_event as line_event

    class FailingResolver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def to_path(self, **_kwargs):
            raise MediaTooLargeError("too large")

    monkeypatch.setattr(line_event, "MediaResolver", FailingResolver)
    batch = await build_line_batch(
        MessageChain(chain=[Image.fromURL("https://example.com/a.gif")])
    )
    assert batch.messages == []


# --------------------------------------------------------------- 出站 mention


@pytest.mark.asyncio
async def test_outbound_mention_uses_textv2_in_group():
    """群聊 / 多人聊天：mention 用 textV2 + substitution 占位符表达。"""
    batch = await build_line_batch(
        MessageChain(chain=[At(qq="U9", name="bob"), Plain(" 早")]),
        allow_mentions=True,
    )
    assert batch.messages == [
        {
            "type": "textV2",
            "text": "{m0} 早",
            "substitution": {
                "m0": {"type": "mention", "mentionee": {"type": "user", "userId": "U9"}}
            },
        }
    ]


@pytest.mark.asyncio
async def test_outbound_mention_degrades_to_plain_text_in_direct_chat():
    """1:1 里 LINE 不支持 mention，带 mentionee 的对象会让整批 400，必须降级。"""
    batch = await build_line_batch(
        MessageChain(chain=[At(qq="U9", name="bob"), Plain(" 早")]),
        allow_mentions=False,
    )
    assert batch.messages == [{"type": "text", "text": "@bob 早"}]


@pytest.mark.asyncio
async def test_outbound_at_all_degrades_to_plain_text_in_direct_chat():
    """@全体在 1:1 同样非法；没有昵称可用时只留正文，绝不放 mentionee 进批次。"""
    batch = await build_line_batch(
        MessageChain(chain=[AtAll(), Plain("通知")]),
        allow_mentions=False,
    )
    assert batch.messages == [{"type": "text", "text": "通知"}]


# --------------------------------------------------------------- 直通与批次


@pytest.mark.asyncio
async def test_flex_and_raw_messages_pass_through_unchanged():
    contents = {"type": "bubble", "body": {"type": "box", "layout": "vertical"}}
    raw = {"type": "sticker", "packageId": "1", "stickerId": "2"}
    batch = await build_line_batch(
        MessageChain(
            chain=[
                LineFlex(alt_text="", contents=contents),
                LineRawMessage(message=raw),
            ]
        )
    )
    # altText 不被隐式改写，contents 与原始对象原样透传。
    assert batch.messages[0] == {"type": "flex", "altText": "", "contents": contents}
    assert batch.messages[1] == raw


@pytest.mark.asyncio
async def test_quick_reply_attaches_to_last_message_and_is_dropped_when_alone():
    quick_reply = LineQuickReply(
        items=[
            LineQuickReplyItem(action=LinePostbackAction(label="buy", data="a=1")),
        ]
    )
    batch = await build_line_batch(MessageChain(chain=[Plain("hi"), quick_reply]))
    messages = finalize_line_messages(batch, quote_store=None, chat_id="C1")
    assert messages[-1]["quickReply"]["items"][0]["action"]["data"] == "a=1"

    alone = await build_line_batch(MessageChain(chain=[quick_reply]))
    assert finalize_line_messages(alone, quote_store=None, chat_id="C1") == []


@pytest.mark.asyncio
async def test_outbound_file_component_is_skipped():
    from astrbot.api.message_components import File

    batch = await build_line_batch(
        MessageChain(chain=[Plain("here"), File(name="a.pdf", file="/tmp/a.pdf")])
    )
    assert [m["type"] for m in batch.messages] == ["text"]


# --------------------------------------------------------------- Flex 媒体


def _write_png(path, size: tuple[int, int]):
    """在 path 写一张指定尺寸的 PNG，返回该路径。"""
    from PIL import Image as PILImage

    PILImage.new("RGB", size, (30, 144, 255)).save(path, "PNG")
    return path


@pytest.fixture
def flex_media_env(tmp_path, monkeypatch):
    """把 Flex 媒体路径的落盘与上传都收进 tmp_path，并记录每次上传。

    只替换上传这一层：物化、转码、尺寸收敛都跑真实实现 —— 这些正是要测的东西。
    """
    from astrbot.core.platform.sources.line import line_event
    from astrbot.core.utils import media_utils

    monkeypatch.setattr(line_media, "get_astrbot_temp_path", lambda: str(tmp_path))
    monkeypatch.setattr(media_utils, "get_astrbot_temp_path", lambda: str(tmp_path))

    uploads: list[str] = []

    async def fake_upload(path, chain, *, mime_type=None):
        uploads.append(path)
        return f"https://cdn.example.com/{Path(path).name}"

    monkeypatch.setattr(line_event, "resolve_public_media_url", fake_upload)
    return uploads


@pytest.mark.asyncio
async def test_flex_media_refs_are_replaced_with_public_urls(tmp_path, flex_media_env):
    """占位符按整串精确匹配替换；普通字符串、未被引用的 key、原 contents 都不受影响。"""
    image = Image.fromFileSystem(_write_png(tmp_path / "a.png", (400, 300)))
    contents = {
        "type": "bubble",
        "hero": {"type": "image", "url": LineFlex.ref("hero")},
        "body": {
            "type": "box",
            "layout": "baseline",
            "contents": [
                {"type": "icon", "url": LineFlex.ref("badge")},
                {"type": "text", "text": "astrbot-media://hero is a literal here"},
                {"type": "text", "text": "https://example.com/keep.png"},
            ],
        },
    }
    flex = LineFlex(
        alt_text="B50",
        media={
            "hero": image,
            "badge": LineFlexMedia(media=image, profile="icon"),
            "unused": Image.fromFileSystem(_write_png(tmp_path / "b.png", (10, 10))),
        },
        contents=contents,
    )

    batch = await build_line_batch(MessageChain(chain=[flex]), ["r2"])
    body = batch.messages[0]["contents"]["body"]["contents"]

    assert batch.messages[0]["contents"]["hero"]["url"].startswith("https://cdn.")
    assert body[0]["url"].startswith("https://cdn.")
    # 占位符出现在长字符串里只是普通文本，不做子串替换。
    assert body[1]["text"] == "astrbot-media://hero is a literal here"
    assert body[2]["text"] == "https://example.com/keep.png"
    # 原 contents 不被改写：同一个组件二次发送不会拿到上一次的 URL。
    assert contents["hero"]["url"] == LineFlex.ref("hero")
    # 未被引用的 key 不物化、不上传。
    assert not any("b.png" in path for path in flex_media_env)


def test_flex_media_key_charset_is_enforced():
    """占位符必须整串可判定：非法 key 永远匹配不上，构造时就得报错而不是发出去。"""
    assert LineFlex.parse_ref(LineFlex.ref("hero_1-a")) == "hero_1-a"
    assert LineFlex.parse_ref("astrbot-media://hero is prose") is None
    assert LineFlex.parse_ref("astrbot-media://") is None
    assert LineFlex.parse_ref("https://example.com/a.png") is None
    assert LineFlex.parse_ref(42) is None
    with pytest.raises(ValueError):
        LineFlex.ref("hero image")


@pytest.mark.asyncio
async def test_flex_media_profiles_converge_to_line_limits(tmp_path, monkeypatch):
    """image / icon 有 1024×1024 px 硬上限（普通 image 消息没有），original 不受约束。

    超限同样是整批 400，因此「已经是 PNG 且不大」还不足以短路，尺寸也得在范围内。
    """
    from PIL import Image as PILImage

    monkeypatch.setattr(line_media, "get_astrbot_temp_path", lambda: str(tmp_path))
    oversized = str(_write_png(tmp_path / "big.png", (2000, 1200)))

    for profile, max_edge in (("image", 1024), ("icon", 512)):
        prepared = await line_media.prepare_flex_media(oversized, profile)
        assert prepared is not None
        with PILImage.open(prepared) as opened:
            assert max(opened.size) <= max_edge
        assert (
            line_media.detect_file_mime_type(prepared)
            in line_media.LINE_IMAGE_MIME_TYPES
        )

    # original 只被 uri action 打开、不由 LINE 渲染，因此原样返回，不转码不缩放。
    assert await line_media.prepare_flex_media(oversized, "original") == oversized
    # 未知 profile 不猜默认值：跳过，避免把不合规的对象放进批次。
    assert await line_media.prepare_flex_media(oversized, "bogus") is None
    assert (
        await line_media.prepare_flex_media(str(tmp_path / "nope.png"), "image") is None
    )


@pytest.mark.asyncio
async def test_flex_original_profile_serves_the_full_size_image(
    tmp_path, flex_media_env
):
    """「Flex 里看缩略版、点开看原图」：两个 profile 产出两个不同的 URL。"""
    image = Image.fromFileSystem(_write_png(tmp_path / "big.png", (2000, 1200)))
    flex = LineFlex(
        alt_text="B50",
        media={"hero": image, "full": LineFlexMedia(media=image, profile="original")},
        contents={
            "type": "bubble",
            "hero": {
                "type": "image",
                "url": LineFlex.ref("hero"),
                "action": {"type": "uri", "uri": LineFlex.ref("full")},
            },
        },
    )

    batch = await build_line_batch(MessageChain(chain=[flex]), None)
    hero = batch.messages[0]["contents"]["hero"]
    assert hero["url"] != hero["action"]["uri"]
    assert hero["action"]["uri"].endswith("/big.png")  # 原图原样上传
    assert len(flex_media_env) == 2


@pytest.mark.asyncio
async def test_flex_media_is_localized_and_uploaded_once_per_rendition(
    tmp_path, flex_media_env, monkeypatch
):
    """同一张图被多个 key 引用时只物化一次；两个 profile 落到同一文件时只上传一次。"""
    from astrbot.core.platform.sources.line import line_event

    localized: list[str] = []
    real_localize = line_event._localize_image

    async def spy_localize(media_ref: str):
        localized.append(media_ref)
        return await real_localize(media_ref)

    monkeypatch.setattr(line_event, "_localize_image", spy_localize)

    # 800×600 已在 1024 以内且是 PNG，image 与 original 都短路返回同一路径。
    image = Image.fromFileSystem(_write_png(tmp_path / "small.png", (800, 600)))
    flex = LineFlex(
        alt_text="B50",
        media={
            "a": image,
            "b": image,
            "full": LineFlexMedia(media=image, profile="original"),
        },
        contents={
            "type": "bubble",
            "hero": {
                "type": "image",
                "url": LineFlex.ref("a"),
                "action": {"type": "uri", "uri": LineFlex.ref("full")},
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "image", "url": LineFlex.ref("b")}],
            },
        },
    )

    batch = await build_line_batch(MessageChain(chain=[flex]), None)
    contents = batch.messages[0]["contents"]
    assert len(localized) == 1
    assert len(flex_media_env) == 1
    # 一次上传，三处复用同一 URL。
    urls = {
        contents["hero"]["url"],
        contents["hero"]["action"]["uri"],
        contents["body"]["contents"][0]["url"],
    }
    assert len(urls) == 1


@pytest.mark.asyncio
async def test_flex_degrades_to_alt_text_when_media_is_unavailable(
    tmp_path, monkeypatch
):
    """缺图的半成品 Flex 会让整批 400，因此整条降级为 alt_text —— 那正是它的用途。"""
    from astrbot.core.platform.sources.line import line_event

    monkeypatch.setattr(line_media, "get_astrbot_temp_path", lambda: str(tmp_path))

    async def no_url(*_args, **_kwargs):
        return None

    monkeypatch.setattr(line_event, "resolve_public_media_url", no_url)
    image = Image.fromFileSystem(_write_png(tmp_path / "a.png", (400, 300)))
    contents = {
        "type": "bubble",
        "hero": {"type": "image", "url": LineFlex.ref("hero")},
    }

    batch = await build_line_batch(
        MessageChain(
            chain=[LineFlex(alt_text="B50", media={"hero": image}, contents=contents)]
        )
    )
    assert batch.messages == [{"type": "text", "text": "B50"}]

    # alt_text 为空则连降级形态都没有，整个组件跳过。
    empty_alt = await build_line_batch(
        MessageChain(
            chain=[LineFlex(alt_text="", media={"hero": image}, contents=contents)]
        )
    )
    assert empty_alt.messages == []


@pytest.mark.asyncio
async def test_flex_with_unresolvable_media_entry_degrades(tmp_path, flex_media_env):
    """引用了不存在的 key、或 media 里放的不是 Image：都不允许把占位符发出去。"""
    contents = {
        "type": "bubble",
        "hero": {"type": "image", "url": LineFlex.ref("hero")},
    }

    missing = await build_line_batch(
        MessageChain(chain=[LineFlex(alt_text="missing", contents=contents)])
    )
    assert missing.messages == [{"type": "text", "text": "missing"}]

    wrong_type = await build_line_batch(
        MessageChain(
            chain=[
                LineFlex(
                    alt_text="wrong",
                    media={"hero": "https://example.com/a.png"},
                    contents=contents,
                )
            ]
        )
    )
    assert wrong_type.messages == [{"type": "text", "text": "wrong"}]
    assert flex_media_env == []


@pytest.mark.asyncio
async def test_raw_message_media_refs_are_not_resolved(tmp_path, flex_media_env):
    """LineRawMessage 是逃生口：严格原样透传，不做任何媒体解析。"""
    raw = {"type": "image", "originalContentUrl": LineFlex.ref("hero")}
    batch = await build_line_batch(MessageChain(chain=[LineRawMessage(message=raw)]))
    assert batch.messages == [raw]
    assert flex_media_env == []


# --------------------------------------------------------------- send_typing


@pytest.mark.asyncio
async def test_send_typing_only_in_direct_chat():
    """loading 动画的 chatId 只接受用户 ID，群聊 / 多人聊天必须静默跳过。"""
    adapter = make_adapter()
    calls: list[tuple] = []

    async def fake_loading(chat_id: str, seconds: int) -> bool:
        calls.append((chat_id, seconds))
        return True

    adapter.line_api.show_loading_animation = fake_loading  # type: ignore[method-assign]

    direct = await adapter.convert_message(text_event("e1"))
    assert direct is not None
    await adapter.create_event(direct).send_typing()
    assert calls == [("U1", 20)]

    group_event = text_event("e2")
    group_event["source"] = {"type": "group", "groupId": "G1", "userId": "U1"}
    group = await adapter.convert_message(group_event)
    assert group is not None
    group_event_obj = adapter.create_event(group)
    await group_event_obj.send_typing()
    # 群聊场景不发起请求，也不报错。
    assert calls == [("U1", 20)]
    await group_event_obj.stop_typing()


@pytest.mark.asyncio
async def test_send_typing_uses_configured_seconds(monkeypatch):
    """时长可由 platform_specific.line.pre_ack_loading.seconds 覆盖；非法值退回默认。"""
    import astrbot.core.platform.sources.line.line_event as line_event

    adapter = make_adapter()
    calls: list[tuple] = []

    async def fake_loading(chat_id: str, seconds: int) -> bool:
        calls.append((chat_id, seconds))
        return True

    adapter.line_api.show_loading_animation = fake_loading  # type: ignore[method-assign]
    direct = await adapter.convert_message(text_event("e1"))
    assert direct is not None

    def set_conf(value):
        monkeypatch.setitem(
            line_event.astrbot_config,
            "platform_specific",
            {"line": {"pre_ack_loading": {"enable": True, "seconds": value}}},
        )

    set_conf(60)
    await adapter.create_event(direct).send_typing()
    # 规整（5~60、5 的倍数）由 show_loading_animation 负责，这里原样透传。
    set_conf(0)
    await adapter.create_event(direct).send_typing()
    set_conf("abc")
    await adapter.create_event(direct).send_typing()
    assert calls == [("U1", 60), ("U1", 20), ("U1", 20)]


@pytest.mark.asyncio
async def test_preprocess_stage_pre_ack_loading_is_opt_in():
    """预回应加载动画默认关闭；开启后仅对 line 且明确唤醒的事件触发。"""
    from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage

    class FakeEvent:
        def __init__(self, platform: str, woken: bool) -> None:
            self.platform = platform
            self.is_at_or_wake_command = woken
            self.typing = 0

        def get_platform_name(self) -> str:
            return self.platform

        async def send_typing(self) -> None:
            self.typing += 1

        async def react(self, _emoji) -> None:
            pass

        def get_messages(self) -> list:
            return []

    async def run(config: dict, event) -> None:
        stage = PreProcessStage()
        # 只装本用例会走到的依赖：STT 关闭后 plugin_manager / ctx 都不会被触碰。
        stage.config = config  # type: ignore[attr-defined]
        stage.platform_settings = {}
        stage.stt_settings = {}
        result = await stage.process(event)
        if result is not None:
            async for _ in result:
                pass

    enabled = {"platform_specific": {"line": {"pre_ack_loading": {"enable": True}}}}
    disabled = {"platform_specific": {"line": {"pre_ack_loading": {"enable": False}}}}

    woken = FakeEvent("line", True)
    await run(enabled, woken)
    assert woken.typing == 1

    not_woken = FakeEvent("line", False)
    await run(enabled, not_woken)
    assert not_woken.typing == 0

    off = FakeEvent("line", True)
    await run(disabled, off)
    assert off.typing == 0

    # 其它平台不受这个开关影响（WebChat 把 send_typing 用作 LLM run 信号）。
    other = FakeEvent("webchat", True)
    await run(enabled, other)
    assert other.typing == 0


@pytest.mark.asyncio
async def test_get_client_returns_line_api():
    adapter = make_adapter()
    assert adapter.get_client() is adapter.line_api


# ------------------------------------------------------------ worker 生命周期


@pytest.mark.asyncio
async def test_terminate_before_run_leaves_no_orphan_workers():
    """terminate() 先于 run() 执行时不能再拉起 worker：没人取消它们就是泄漏任务。"""
    adapter = make_adapter()

    await adapter.terminate()
    assert adapter.line_api._closed

    await adapter.run()
    assert adapter._workers == []
    # 事件循环里不应留下 LINE worker 任务。
    assert not [
        task
        for task in asyncio.all_tasks()
        if "_event_worker" in task.get_coro().__qualname__
    ]


@pytest.mark.asyncio
async def test_run_stops_its_workers_when_cancelled():
    """run() 被取消时也要收掉自己拉起的 worker。"""
    adapter = make_adapter()
    run_task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0)
    workers = list(adapter._workers)
    assert len(workers) == 4

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await asyncio.sleep(0)
    assert all(worker.cancelled() or worker.done() for worker in workers)


@pytest.mark.asyncio
async def test_stop_workers_is_idempotent():
    adapter = make_adapter()
    run_task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0)
    workers = list(adapter._workers)

    await adapter.terminate()
    await run_task
    await adapter._stop_workers()  # 再调一次不应报错
    assert adapter._workers == []
    assert all(worker.done() for worker in workers)


# ------------------------------------------------------------------- 昵称缓存


@pytest.mark.asyncio
async def test_nickname_cache_evicts_expired_and_cold_entries(monkeypatch):
    import astrbot.core.platform.sources.line.line_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "_NICKNAME_CACHE_CAPACITY", 3)
    adapter = make_adapter()

    for index in range(5):
        await adapter._resolve_nickname("user", f"U{index}", "", "")
    # 容量上限生效，最久未命中的先出。
    assert len(adapter._nickname_cache) == 3
    assert ("user", "U0") not in adapter._nickname_cache
    assert ("user", "U4") in adapter._nickname_cache

    # 过期项在下一次写入时被清掉，不会永久留存。
    stale_key = ("user", "U4")
    adapter._nickname_cache[stale_key] = (
        time.time() - adapter_module._NICKNAME_TTL_SECONDS - 1,
        "Stale",
    )
    assert await adapter._resolve_nickname("user", "U4", "", "") == "Display Name"
    await adapter._resolve_nickname("user", "U9", "", "")
    cached_at, name = adapter._nickname_cache[stale_key]
    assert name == "Display Name"
    assert time.time() - cached_at < adapter_module._NICKNAME_TTL_SECONDS
