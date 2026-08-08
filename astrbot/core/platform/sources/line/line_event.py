"""LINE 消息事件：出站组件 → LINE 消息对象，以及 pipeline 结束后的一次性 reply。

发送模型：多次 send() 只缓冲，pipeline 结束时用 reply token 发送一次。没有任何 push
兜底、没有任何超时抢发：reply 失败就是失败，只按类别记结构化日志。

进入批次的每个消息对象都必须是适配器能判断为合法的对象 —— 一次 reply 里只要有一个非法对象，
LINE 就返回 400 让整批发不出去。因此媒体收敛不下去、URL 拿不到、时长探测不到的组件
一律跳过并 warning，而不是「放进去让 LINE 判」。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.core import astrbot_config
from astrbot.core.platform.astr_message_event import MessageType
from astrbot.core.utils.media_utils import MediaResolver

from .components import LineFlex, LineFlexMedia, LineQuickReply, LineRawMessage
from .line_api import LineAPIClient, LineSendResult
from .line_cache import LineQuoteStore
from .line_media import (
    EXTERNAL_IMAGE_LIMIT,
    extract_local_video_cover,
    prepare_flex_media,
    prepare_line_audio,
    prepare_line_image,
    prepare_line_video,
    resolve_audio_duration,
    resolve_public_media_url,
)
from .line_text import truncate_utf16

LINE_MAX_MESSAGES_PER_REPLY = 5
LINE_TEXT_MAX_UTF16 = 5000
LOADING_SECONDS = 20
"""loading 动画时长（秒）的默认值。LINE 要求 5~60 且为 5 的倍数；到时自动消失或被新消息顶掉。

可用 platform_specific.line.pre_ack_loading.seconds 覆盖。长耗时指令把它调大即可 ——
LINE 没有续期接口，动画一旦到时就消失，之后回复才到会留下一段空窗。
"""

_TEXT_MESSAGE_TYPES = frozenset({"text", "textV2"})


@dataclass
class LineOutboundBatch:
    """一次 send() 转换出的实体消息与控制信息。

    控制信息（Quick Reply、引用目标）不产生实体消息对象，只修饰最终发送批次，
    因此必须累积到 flush 时才消费。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    quick_reply: dict[str, Any] | None = None
    quote_message_id: str | None = None


async def build_line_batch(
    message_chain: MessageChain,
    image_host_chain: list[str] | None = None,
    *,
    allow_mentions: bool = False,
) -> LineOutboundBatch:
    """把 AstrBot 消息链转换成 LINE 消息对象与控制信息。

    Args:
        message_chain: 待发送的消息链。
        image_host_chain: 图床后端 id 优先链。
        allow_mentions: 目标是否为群聊 / 多人聊天。LINE 只在这两种目标上支持原生
            mention，1:1 里带 mentionee 的对象会让整批消息 400，因此默认关闭。

    Returns:
        转换结果；无法产出合法对象的组件已被跳过（各自记 warning）。
    """
    batch = LineOutboundBatch()
    text_run: list[BaseMessageComponent] = []

    def flush_text_run() -> None:
        if not text_run:
            return
        batch.messages.extend(_text_run_to_messages(text_run, allow_mentions))
        text_run.clear()

    for segment in message_chain.chain:
        if isinstance(segment, (Plain, At)):
            text_run.append(segment)
            continue

        flush_text_run()

        if isinstance(segment, LineQuickReply):
            quick_reply = segment.to_line_dict()
            if quick_reply:
                if batch.quick_reply is not None:
                    logger.warning(
                        "[LINE] multiple quick replies in one send, keeping the last one."
                    )
                batch.quick_reply = quick_reply
            continue

        if isinstance(segment, Reply):
            message_id = str(segment.id).strip()
            if message_id:
                if batch.quote_message_id is not None:
                    logger.warning(
                        "[LINE] multiple quote targets in one send, keeping the last one."
                    )
                batch.quote_message_id = message_id
            continue

        message_object = await _component_to_message_object(segment, image_host_chain)
        if message_object:
            batch.messages.append(message_object)

    flush_text_run()
    return batch


def _text_run_to_messages(
    run: list[BaseMessageComponent], allow_mentions: bool
) -> list[dict[str, Any]]:
    """把一段连续的 Plain / At 转成文本消息对象。

    切法：一个 Plain 一条消息（保持既有的气泡表现），紧随其后的正文与 At 合并成同一条
    —— 原生 mention 必须与正文同处一条消息，@某人 正文 才是用户预期的一个气泡。
    """
    messages: list[dict[str, Any]] = []
    chunk: list[BaseMessageComponent] = []
    for comp in run:
        # 一个 chunk 里最多一个 Plain：正文出现后即封口，At 归入下一条。
        if any(isinstance(c, Plain) for c in chunk):
            message = _build_text_message(chunk, allow_mentions)
            if message:
                messages.append(message)
            chunk = []
        chunk.append(comp)
    if chunk:
        message = _build_text_message(chunk, allow_mentions)
        if message:
            messages.append(message)
    return messages


def _build_text_message(
    chunk: list[BaseMessageComponent], allow_mentions: bool
) -> dict[str, Any] | None:
    """把一段（At* + 最多一个 Plain）转成 text 或带 mention 的 textV2。"""
    if not any(isinstance(comp, At) for comp in chunk):
        text = truncate_utf16(
            "".join(comp.text for comp in chunk if isinstance(comp, Plain)).strip(),
            LINE_TEXT_MAX_UTF16,
        )
        return {"type": "text", "text": text} if text else None
    return _build_mention_message(chunk, allow_mentions)


def _build_mention_message(
    run: list[BaseMessageComponent], allow_mentions: bool
) -> dict[str, Any] | None:
    """把含 At 的文本段转成带原生 mention 的 textV2。

    textV2 用 {占位符} + substitution 表达 mention，不需要偏移量 ——
    这样 mention 之前含 emoji 时也不会错位（按码点算偏移正是旧实现的 bug）。

    两种情况必须降级为 @昵称 的纯文本：目标不是群聊 / 多人聊天（LINE 只在这两种目标上
    支持 mention，1:1 里带 mentionee 会让整批 400），以及正文里出现 { / }
    导致无法可靠区分占位符与字面量。
    """
    plain_texts = [comp.text for comp in run if isinstance(comp, Plain)]
    braces_in_text = any("{" in text or "}" in text for text in plain_texts)
    degrade = braces_in_text or not allow_mentions

    parts: list[str] = []
    substitution: dict[str, Any] = {}
    for comp in run:
        if isinstance(comp, Plain):
            parts.append(comp.text)
            continue
        target = str(getattr(comp, "qq", "") or "").strip()
        name = str(getattr(comp, "name", "") or "").strip()
        if degrade or not target:
            parts.append(f"@{name}" if name else "")
            continue
        mentionee: dict[str, Any] = (
            {"type": "all"} if target == "all" else {"type": "user", "userId": target}
        )
        key = f"m{len(substitution)}"
        substitution[key] = {"type": "mention", "mentionee": mentionee}
        parts.append("{" + key + "}")

    text = truncate_utf16("".join(parts).strip(), LINE_TEXT_MAX_UTF16)
    if not text:
        return None
    # 截断可能把尾部的占位符切掉，留下的 substitution 键必须与正文一致。
    substitution = {
        key: value for key, value in substitution.items() if "{" + key + "}" in text
    }
    if not substitution:
        return {"type": "text", "text": text}
    return {"type": "textV2", "text": text, "substitution": substitution}


async def _component_to_message_object(
    segment: BaseMessageComponent,
    chain: list[str] | None,
) -> dict[str, Any] | None:
    """把单个非文本组件转换成 LINE 消息对象；无法产出合法对象时返回 None。"""
    if isinstance(segment, LineRawMessage):
        return segment.to_line_dict()

    if isinstance(segment, LineFlex):
        return await _flex_to_message(segment, chain)

    if isinstance(segment, Image):
        return await _image_to_message(segment, chain)

    if isinstance(segment, Record):
        return await _record_to_message(segment, chain)

    if isinstance(segment, Video):
        return await _video_to_message(segment, chain)

    if isinstance(segment, File):
        # LINE 的出站消息类型里没有 file：官方账号无法向用户发送文件消息。
        # 构造出来必被拒，并且会连带整批消息一起失败。
        logger.warning(
            "[LINE] outbound file messages are not supported by LINE, component skipped: %s",
            segment.name or "file",
        )
        return None

    logger.debug("[LINE] unsupported outbound component skipped: %s", segment.type)
    return None


# ---------------------------------------------------------------- Flex 媒体


async def _flex_to_message(
    segment: LineFlex, chain: list[str] | None
) -> dict[str, Any] | None:
    """Flex：把 contents 里的媒体占位符换成公网 URL，其余部分原样透传。

    没有占位符时是零开销直通，与「Flex 结构由插件负责」的边界一致 —— 只有插件显式用
    LineFlex.ref() 占位并在 media 里给出 Image 才会触发转换。

    任一被引用的媒体拿不到 URL 就整条降级为 alt_text 文本：alt_text 本就是 LINE 为
    「Flex 无法渲染时显示什么」准备的字段，而把缺图的半成品 Flex 放进批次会让整批 400。
    """
    referenced = _collect_flex_media_refs(segment.contents)
    unused = set(segment.media) - referenced
    if unused:
        logger.debug(
            "[LINE] flex media key(s) not referenced in contents, ignored: %s",
            ", ".join(sorted(unused)),
        )
    if not referenced:
        return segment.to_line_dict()

    # 两级去重，只在这一条 Flex 的转换过程内有效：
    # 同一个 Image 对象被多个 key 引用时只物化一次（base64 解码 / 外链下载都不便宜）；
    # 不同 profile 收敛到同一个文件时（例如源图本来就在 1024 以内，image 与 original
    # 都短路返回原路径）只上传一次。
    localized: dict[int, str | None] = {}
    uploaded: dict[str, str | None] = {}

    resolved: dict[str, str] = {}
    for key in sorted(referenced):
        url = await _resolve_flex_media(segment, key, chain, localized, uploaded)
        if not url:
            logger.warning(
                "[LINE] flex media %r unavailable, message degraded to alt text.", key
            )
            return _flex_alt_text_message(segment)
        resolved[key] = url

    return segment.to_line_dict(_substitute_flex_media(segment.contents, resolved))


def _collect_flex_media_refs(node: object) -> set[str]:
    """递归收集 contents 里所有媒体占位符的 key。

    不做字段白名单：Flex 结构由插件负责，image.url / icon.url / video.previewUrl /
    carousel 与 box 的 contents 数组都可能出现占位符，按位置枚举必然漏。
    """
    if isinstance(node, dict):
        keys: set[str] = set()
        for value in node.values():
            keys |= _collect_flex_media_refs(value)
        return keys
    if isinstance(node, list):
        keys = set()
        for value in node:
            keys |= _collect_flex_media_refs(value)
        return keys
    key = LineFlex.parse_ref(node)
    return {key} if key else set()


async def _resolve_flex_media(
    segment: LineFlex,
    key: str,
    chain: list[str] | None,
    localized: dict[int, str | None],
    uploaded: dict[str, str | None],
) -> str | None:
    """把 media[key] 物化、按 profile 收敛、上传，返回公网 URL；任一步失败返回 None。

    Args:
        segment: 待转换的 Flex 组件。
        key: media 映射里的键。
        chain: 图床后端 id 优先链。
        localized: 本次转换内的物化缓存（按 Image 对象身份）。
        uploaded: 本次转换内的上传缓存（按收敛产物路径）。
    """
    entry = segment.media.get(key)
    if entry is None:
        logger.warning("[LINE] flex references media %r but it is not provided.", key)
        return None

    if isinstance(entry, LineFlexMedia):
        media, profile = entry.media, entry.profile
    else:
        media, profile = entry, "image"
    if not isinstance(media, Image):
        logger.warning(
            "[LINE] flex media %r is not an Image component: %r", key, type(media)
        )
        return None

    if id(media) not in localized:
        localized[id(media)] = await _localize_image(
            (media.url or media.file or "").strip()
        )
    local_path = localized[id(media)]
    if not local_path:
        return None

    prepared = await prepare_flex_media(local_path, profile)
    if not prepared:
        return None

    if prepared not in uploaded:
        uploaded[prepared] = await resolve_public_media_url(prepared, chain)
    url = uploaded[prepared]

    # 包装在上传缓存之后：同一个文件被两个 key 引用、但只有其中一个要包装时，
    # 缓存里存的必须还是裸的公网 URL。
    if url and isinstance(entry, LineFlexMedia) and entry.url_template:
        return entry.url_template.replace("{url}", quote(url, safe=""))
    return url


def _substitute_flex_media(node: object, resolved: dict[str, str]) -> Any:
    """深拷贝 contents，把占位符字符串替换成 URL。原 contents 不被改写。"""
    if isinstance(node, dict):
        return {k: _substitute_flex_media(v, resolved) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_flex_media(v, resolved) for v in node]
    key = LineFlex.parse_ref(node)
    if key and key in resolved:
        return resolved[key]
    return node


def _flex_alt_text_message(segment: LineFlex) -> dict[str, Any] | None:
    """Flex 的降级形态：把 alt_text 作为普通文本消息发出；alt_text 为空则跳过。"""
    text = truncate_utf16(segment.alt_text.strip(), LINE_TEXT_MAX_UTF16)
    if not text:
        logger.warning("[LINE] flex has no alt text to degrade to, component skipped.")
        return None
    return {"type": "text", "text": text}


# ---------------------------------------------------------------- 媒体组件


async def _image_to_message(
    segment: Image, chain: list[str] | None
) -> dict[str, Any] | None:
    """图片：外链也要下载后走与本地相同的收敛流程，原图 URL 同时充当预览图。

    LINE 文档给 previewImageUrl 标了 1 MB 上限，但实际上只是摆设
    """
    local_path = await _localize_image((segment.url or segment.file or "").strip())
    if not local_path:
        return None

    original_path = await prepare_line_image(local_path)
    if not original_path:
        return None

    url = await resolve_public_media_url(original_path, chain)
    if not url:
        logger.warning("[LINE] no usable URL for image, component skipped.")
        return None

    return {
        "type": "image",
        "originalContentUrl": url,
        "previewImageUrl": url,
    }


async def _localize_image(media_ref: str) -> str | None:
    """把图片引用落成本地文件；失败即跳过，不直用外链。

    外链图片交给公共 MediaResolver 物化，并给它一个字节上限：外链 GIF/WebP 会被 LINE
    拒绝而导致整批 400，而格式只有下载下来才能确认。

    Args:
        media_ref: 图片引用（本地路径、file:// URI、HTTP(S) URL、base64 等）。

    Returns:
        本地文件路径，或失败时 None。
    """
    if not media_ref:
        return None
    try:
        return await MediaResolver(
            media_ref,
            media_type="image",
            max_bytes=EXTERNAL_IMAGE_LIMIT,
        ).to_path()
    except Exception as e:
        logger.warning(
            "[LINE] resolve image failed, component skipped: %s (%s)", media_ref, e
        )
        return None


async def _record_to_message(
    segment: Record, chain: list[str] | None
) -> dict[str, Any] | None:
    """音频：外链直用（格式与体积由调用方负责），但必须拿到准确的毫秒时长。

    LINE 的 duration 是必填字段，播放器按它渲染进度条，错值是用户可见的错误。外链时
    时长只能由调用方通过 Record.duration 给出 —— 适配器不会为了探测时长去访问外链。
    """
    candidate = (segment.url or segment.file or "").strip()
    if candidate.startswith("http://") or candidate.startswith("https://"):
        if not candidate.startswith("https://"):
            logger.warning(
                "[LINE] external audio URL must be HTTPS, component skipped: %s",
                candidate,
            )
            return None
        duration = segment.duration if isinstance(segment.duration, int) else 0
        if duration <= 0:
            logger.warning(
                "[LINE] external audio has no duration (set Record.duration in ms), "
                "component skipped: %s",
                candidate,
            )
            return None
        return {
            "type": "audio",
            "originalContentUrl": candidate,
            "duration": duration,
        }

    try:
        local_path = await segment.convert_to_file_path()
    except Exception as e:
        logger.warning("[LINE] resolve local audio failed, component skipped: %s", e)
        return None

    prepared = await prepare_line_audio(local_path)
    if not prepared:
        return None
    duration = (
        segment.duration
        if isinstance(segment.duration, int) and segment.duration > 0
        else await resolve_audio_duration(prepared)
    )
    if not duration:
        logger.warning("[LINE] cannot determine audio duration, component skipped.")
        return None
    url = await resolve_public_media_url(prepared, chain)
    if not url:
        logger.warning("[LINE] no usable URL for audio, component skipped.")
        return None
    return {"type": "audio", "originalContentUrl": url, "duration": duration}


async def _video_to_message(
    segment: Video, chain: list[str] | None
) -> dict[str, Any] | None:
    """视频：外链直用本体，但缺预览图就跳过 —— 绝不为抽帧下载整个外链视频。"""
    candidate = (segment.file or segment.url or "").strip()
    is_external = candidate.startswith("http://") or candidate.startswith("https://")

    if is_external:
        if not candidate.startswith("https://"):
            logger.warning(
                "[LINE] external video URL must be HTTPS, component skipped: %s",
                candidate,
            )
            return None
        video_url = candidate
        cover_source = (segment.cover or "").strip()
        if not cover_source:
            logger.warning(
                "[LINE] external video has no preview image, component skipped: %s",
                candidate,
            )
            return None
        preview_url = await _cover_to_url(cover_source, chain)
    else:
        try:
            local_path = await segment.convert_to_file_path()
        except Exception as e:
            logger.warning(
                "[LINE] resolve local video failed, component skipped: %s", e
            )
            return None
        prepared = await prepare_line_video(local_path)
        if not prepared:
            return None
        video_url = await resolve_public_media_url(prepared, chain) or ""
        if not video_url:
            logger.warning("[LINE] no usable URL for video, component skipped.")
            return None
        cover_source = (segment.cover or "").strip()
        if cover_source:
            preview_url = await _cover_to_url(cover_source, chain)
        else:
            # 本地文件抽帧没有下载代价。
            cover_path = await extract_local_video_cover(prepared)
            preview_url = (
                await resolve_public_media_url(cover_path, chain)
                if cover_path
                else None
            )

    if not preview_url:
        logger.warning("[LINE] no usable preview image for video, component skipped.")
        return None
    return {
        "type": "video",
        "originalContentUrl": video_url,
        "previewImageUrl": preview_url,
    }


async def _cover_to_url(cover_source: str, chain: list[str] | None) -> str | None:
    """视频封面按图片规则处理：外链先受限下载，再收敛成合法 JPEG/PNG。

    只保证格式与 10 MB 上限
    """
    local_path = await _localize_image(cover_source)
    if not local_path:
        return None
    cover_path = await prepare_line_image(local_path)
    if not cover_path:
        return None
    return await resolve_public_media_url(cover_path, chain)


# ---------------------------------------------------------------- 批次收尾


def finalize_line_messages(
    batch: LineOutboundBatch,
    *,
    quote_store: LineQuoteStore | None,
    chat_id: str,
) -> list[dict[str, Any]]:
    """把累积结果收敛成最终发送批次：截到 5 条、挂 Quick Reply、挂引用凭据。

    Args:
        batch: 累积后的批次。
        quote_store: 引用凭据缓存（按聊天作用域查表）。
        chat_id: 当前聊天 id，用于 quoteToken 的作用域校验。

    Returns:
        可直接交给 reply / push 的 messages 数组。
    """
    messages = [
        dict(message) for message in batch.messages[:LINE_MAX_MESSAGES_PER_REPLY]
    ]

    if batch.quick_reply is not None:
        if messages:
            # Quick Reply 是消息基类字段，附着在最终批次的最后一条上，不占消息配额。
            messages[-1]["quickReply"] = batch.quick_reply
        else:
            logger.warning(
                "[LINE] quick reply given without any message object, dropped."
            )

    if batch.quote_message_id:
        _attach_quote_token(messages, batch.quote_message_id, quote_store, chat_id)

    return messages


def _attach_quote_token(
    messages: list[dict[str, Any]],
    quote_message_id: str,
    quote_store: LineQuoteStore | None,
    chat_id: str,
) -> None:
    """给本批第一条文本消息挂上 quoteToken；查不到凭据时正文照发。"""
    token = quote_store.get_token(chat_id, quote_message_id) if quote_store else None
    if not token:
        # 跨聊天的引用目标同样落在这里：查不到就不引用，绝不把非法 token 放进批次。
        logger.warning(
            "[LINE] no quote token for message %s in this chat, sending without quote.",
            quote_message_id,
        )
        return
    for message in messages:
        if message.get("type") in _TEXT_MESSAGE_TYPES:
            message["quoteToken"] = token
            return
    logger.warning(
        "[LINE] quote target given but batch has no text message, quote skipped."
    )


def remember_sent_messages(
    sent_payload: list[dict[str, Any]],
    result: LineSendResult,
    *,
    quote_store: LineQuoteStore | None,
    chat_id: str,
) -> None:
    """把发送回执记入缓存：quoteToken 用于之后引用，正文用于恢复被引用内容。

    sentMessages[] 与请求里的 messages 按顺序一一对应，所以能把回执的 message id 配回
    我们刚发出去的对象。这一步不可省：用户引用 bot 发出的文本时，LINE Content API 无法回查
    文本内容，本地缓存是唯一的恢复来源。

    Args:
        sent_payload: 本次实际发出的 messages 数组。
        result: 发送结果。
        quote_store: 引用缓存。
        chat_id: 聊天作用域。
    """
    if quote_store is None:
        return
    for index, sent in enumerate(result.sent_messages):
        quote_store.put_token(chat_id, sent.id, sent.quote_token)
        payload = sent_payload[index] if index < len(sent_payload) else None
        components = _recoverable_components(payload)
        if components:
            quote_store.put_content(chat_id, sent.id, components)


def _recoverable_components(
    payload: dict[str, Any] | None,
) -> list[BaseMessageComponent]:
    """从已发出的消息对象里取出可本地恢复的内容组件。

    只有文本能恢复：媒体消息在缓存里存 URL 没有意义（LINE 拉的是我们给的外链，
    而该外链可能已经过期），Flex 与原始对象也不对应任何 AstrBot 组件。
    """
    if not isinstance(payload, dict):
        return []
    if payload.get("type") not in _TEXT_MESSAGE_TYPES:
        return []
    text = str(payload.get("text", ""))
    return [Plain(text=text)] if text else []


class LineMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str,
        message_obj,
        platform_meta,
        session_id,
        line_api: LineAPIClient,
        image_host_chain: list[str] | None = None,
        quote_store: LineQuoteStore | None = None,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.line_api = line_api
        # 图床后端 id 有序优先链（对应全局 image_host 的 id）；空则用全部已启用后端。
        self._image_host_chain = image_host_chain
        self._quote_store = quote_store

        # LINE 的 reply token 单次有效、官方建议一分钟内使用，且明确要求不要依赖该时限
        # 设计逻辑 —— 因此这里不做任何时限判断，只在 pipeline 结束后发一次。
        raw = message_obj.raw_message
        self._reply_token = (
            str(raw.get("replyToken") or "") if isinstance(raw, dict) else ""
        )
        self._batch = LineOutboundBatch()
        self._reply_dropped = 0
        self._flush_task: asyncio.Task | None = None
        self._flushed = False

    # ------------------------------------------------------------ postback

    def is_postback(self) -> bool:
        """本事件是否来自 postback（Flex / Quick Reply / Rich Menu 按钮点击）。

        Postback 是结构化交互事件，不是消息：message 为空、message_str 为空，
        原始事件保存在 message_obj.raw_message 里。
        """
        raw = self.message_obj.raw_message
        return isinstance(raw, dict) and str(raw.get("type", "")) == "postback"

    def is_callback_event(self) -> bool:
        return self.is_postback()

    def _postback_payload(self) -> dict[str, Any]:
        raw = self.message_obj.raw_message
        if not isinstance(raw, dict):
            return {}
        payload = raw.get("postback")
        return payload if isinstance(payload, dict) else {}

    def get_postback_data(self) -> str | None:
        """取 postback 的 data（最长 300 字符）；非 postback 事件返回 None。"""
        if not self.is_postback():
            return None
        return str(self._postback_payload().get("data", ""))

    def get_postback_params(self) -> dict[str, Any]:
        """取 postback 的 params（日期时间选择器 / rich menu 切换等会带）。"""
        params = self._postback_payload().get("params")
        return dict(params) if isinstance(params, dict) else {}

    # ------------------------------------------------------------ 体验

    async def send_typing(self) -> None:
        """显示 LINE 的 loading 动画（AstrBot 的「输入中」在 LINE 上就是它）。

        仅 1:1 聊天可用 —— LINE 的 chatId 只接受用户 ID，官方明确不能指定群聊或
        多人聊天，因此非 1:1 场景不发起请求、不产生错误。

        """
        if self.message_obj.type != MessageType.FRIEND_MESSAGE:
            logger.debug("[LINE] loading animation skipped for non 1:1 chat.")
            return
        user_id = str(self.message_obj.sender.user_id or "").strip()
        if not user_id:
            return
        configured = (
            astrbot_config.get("platform_specific", {})
            .get("line", {})
            .get("pre_ack_loading", {})
            .get("seconds")
        )
        seconds = (
            configured
            if isinstance(configured, int) and configured > 0
            else LOADING_SECONDS
        )
        # 5~60 且为 5 的倍数的规整由 show_loading_animation 负责。
        await self.line_api.show_loading_animation(user_id, seconds)

    async def stop_typing(self) -> None:
        """LINE 没有提前结束 loading 动画的接口：它到时自动消失，或被新消息顶掉。"""

    # ------------------------------------------------------------ 发送

    async def send(self, message: MessageChain) -> None:
        batch = await build_line_batch(
            message,
            self._image_host_chain,
            # 原生 mention 仅群聊 / 多人聊天可用；1:1 里降级成 @昵称 纯文本。
            allow_mentions=self.message_obj.type != MessageType.FRIEND_MESSAGE,
        )
        self._accumulate(batch)
        await super().send(message)

    def _accumulate(self, batch: LineOutboundBatch) -> None:
        """把本次 send() 的产物累积到缓冲区，并安排 pipeline 结束后的一次性发送。"""
        if self._flushed:
            # pipeline 已结束、reply token 已消耗，无法再回复。
            if batch.messages:
                self._reply_dropped += len(batch.messages)
                logger.warning(
                    "[LINE] reply already sent, %s late message(s) dropped.",
                    len(batch.messages),
                )
            return

        if batch.quick_reply is not None:
            if self._batch.quick_reply is not None:
                logger.warning(
                    "[LINE] quick reply overridden by a later send(), keeping the last one."
                )
            self._batch.quick_reply = batch.quick_reply

        if batch.quote_message_id is not None:
            if self._batch.quote_message_id is not None:
                logger.warning(
                    "[LINE] quote target overridden by a later send(), keeping the last one."
                )
            self._batch.quote_message_id = batch.quote_message_id

        if batch.messages:
            # 超出部分立刻丢弃，不无界占用资源：循环调用 send() 的插件不得把内存吃光。
            remaining = LINE_MAX_MESSAGES_PER_REPLY - len(self._batch.messages)
            if remaining > 0:
                self._batch.messages.extend(batch.messages[:remaining])
            if len(batch.messages) > max(remaining, 0):
                self._reply_dropped += len(batch.messages) - max(remaining, 0)

        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_when_finished())

    async def _flush_when_finished(self) -> None:
        try:
            await self._pipeline_finished.wait()
        finally:
            await self._flush()

    async def _flush(self) -> None:
        if self._flushed:
            return
        self._flushed = True

        batch = self._batch
        self._batch = LineOutboundBatch()
        if self._reply_dropped:
            logger.warning(
                "[LINE] reply limited to %s messages, %s extra segment(s) dropped.",
                LINE_MAX_MESSAGES_PER_REPLY,
                self._reply_dropped,
            )

        messages = finalize_line_messages(
            batch, quote_store=self._quote_store, chat_id=self.session_id
        )
        if not messages:
            return

        if not self._reply_token:
            logger.warning(
                "[LINE] no reply token available, %s message(s) not sent.",
                len(messages),
            )
            return

        result = await self.line_api.reply_message(self._reply_token, messages)
        if result.ok:
            remember_sent_messages(
                messages,
                result,
                quote_store=self._quote_store,
                chat_id=self.session_id,
            )
            return
        # 失败不改变发送通道：不 push 兜底、不重试、不剔除疑似非法对象后重发。
        logger.error(
            "[LINE] reply not delivered (category=%s), %s message(s) lost.",
            result.error_category.value if result.error_category else "unknown",
            len(messages),
        )

    async def send_streaming(
        self,
        generator: AsyncGenerator,
        use_fallback: bool = False,
    ):
        if not use_fallback:
            buffer = None
            async for chain in generator:
                if not buffer:
                    buffer = chain
                else:
                    buffer.chain.extend(chain.chain)
            if not buffer:
                return None
            buffer.squash_plain()
            await self.send(buffer)
            return await super().send_streaming(generator, use_fallback)

        buffer = ""
        pattern = re.compile(r"[^。？！~…]+[。？！~…]+")

        async for chain in generator:
            if isinstance(chain, MessageChain):
                for comp in chain.chain:
                    if isinstance(comp, Plain):
                        buffer += comp.text
                        if any(p in buffer for p in "。？！~…"):
                            buffer = await self.process_buffer(buffer, pattern)
                    else:
                        await self.send(MessageChain(chain=[comp]))
                        await asyncio.sleep(1.5)

        if buffer.strip():
            await self.send(MessageChain([Plain(buffer)]))
        return await super().send_streaming(generator, use_fallback)
