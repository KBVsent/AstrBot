"""LINE 平台适配器。

Webhook 回调只做「校验 + 受理」：签名 / JSON 不合法返回 400，正在关闭返回 503，
队列满返回 503 并记 ERROR，受理成功立刻返回 200。媒体下载与转码都在后台 worker 里做，
HTTP 响应不等待它们。待处理事件走有界队列，不会无界积压。

已接受的取舍：返回 200 后进程崩溃会丢失尚未处理的事件，LINE 默认不补投
（webhook redelivery 需在 Console 显式开启，且官方不保证可靠送达）。本版不引入持久化队列。
"""

import asyncio
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.media_utils import MediaResolver, detect_file_mime_type_async
from astrbot.core.utils.webhook_utils import log_webhook_info

from ...register import register_platform_adapter
from .line_api import LineAPIClient, LineMessageContent
from .line_cache import LineQuoteStore
from .line_event import (
    LineMessageEvent,
    build_line_batch,
    finalize_line_messages,
    remember_sent_messages,
)
from .line_media import INBOUND_DOWNLOAD_LIMIT, QUOTE_LOOKUP_LIMIT
from .line_text import utf16_slice, utf16_split

LINE_CONFIG_METADATA = {
    "channel_access_token": {
        "description": "LINE Channel Access Token",
        "type": "string",
        "hint": "LINE Messaging API 的 channel access token。",
    },
    "channel_secret": {
        "description": "LINE Channel Secret",
        "type": "string",
        "hint": "用于校验 LINE Webhook 签名。",
    },
}

LINE_I18N_RESOURCES = {
    "zh-CN": {
        "channel_access_token": {
            "description": "LINE Channel Access Token",
            "hint": "LINE Messaging API 的 channel access token。",
        },
        "channel_secret": {
            "description": "LINE Channel Secret",
            "hint": "用于校验 LINE Webhook 签名。",
        },
    },
    "en-US": {
        "channel_access_token": {
            "description": "LINE Channel Access Token",
            "hint": "Channel access token for LINE Messaging API.",
        },
        "channel_secret": {
            "description": "LINE Channel Secret",
            "hint": "Used to verify LINE webhook signatures.",
        },
    },
}

_NICKNAME_TTL_SECONDS = 3600.0
_NICKNAME_CACHE_CAPACITY = 2000
_EVENT_QUEUE_MAXSIZE = 200
_EVENT_WORKER_COUNT = 4
_TERMINATE_TIMEOUT_SECONDS = 20.0


def _evict_profile_cache(
    cache: OrderedDict[Any, tuple[float, Any]], now: float
) -> None:
    """清掉过期项，并把缓存压回容量上限内 —— 否则长跑大群会无界增长。

    昵称与界面语言两个缓存共用：都从同一个 profile 端点派生，新鲜度要求一致。
    """
    expired = [
        key
        for key, (cached_at, _) in cache.items()
        if now - cached_at >= _NICKNAME_TTL_SECONDS
    ]
    for key in expired:
        cache.pop(key, None)
    while len(cache) > _NICKNAME_CACHE_CAPACITY:
        # 最久未命中的先出（读命中会 move_to_end）。
        cache.popitem(last=False)


def _local_media_present(components: list) -> bool:
    """组件里引用的本地文件是不是都还在。

    只看本地路径：http(s) 与 base64 引用不落盘，没有失效一说。
    """
    for comp in components:
        ref = str(getattr(comp, "path", "") or getattr(comp, "file", "") or "")
        if not ref or ref.startswith(("http://", "https://", "base64://")):
            continue
        if ref.startswith("file://"):
            ref = url2pathname(urlparse(ref).path)
        if not Path(ref).exists():
            return False
    return True


@register_platform_adapter(
    "line",
    "LINE Messaging API 适配器",
    support_streaming_message=False,
    config_metadata=LINE_CONFIG_METADATA,
    i18n_resources=LINE_I18N_RESOURCES,
)
class LinePlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config["unified_webhook_mode"] = True
        self.destination = "unknown"
        self.settings = platform_settings
        self._event_id_timestamps: dict[str, float] = {}
        self.shutdown_event = asyncio.Event()

        self._inbound_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_MAXSIZE
        )
        self._workers: list[asyncio.Task] = []
        self._workers_lock = asyncio.Lock()
        self._in_flight = 0  # worker 已取出但尚未处理完成的事件数
        self._quote_store = LineQuoteStore()
        self._nickname_cache: OrderedDict[tuple[str, str], tuple[float, str]] = (
            OrderedDict()
        )
        # user_id -> (取回时刻, BCP 47 语言码或 None)。缓存 None 是有意的，见 _resolve_language。
        self._language_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()

        channel_access_token = str(platform_config.get("channel_access_token", ""))
        channel_secret = str(platform_config.get("channel_secret", ""))
        if not channel_access_token or not channel_secret:
            raise ValueError(
                "LINE 适配器需要 channel_access_token 和 channel_secret。",
            )

        self.line_api = LineAPIClient(
            channel_access_token=channel_access_token,
            channel_secret=channel_secret,
        )

    # ------------------------------------------------------------ 生命周期

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="line",
            description="LINE Messaging API 适配器",
            id=cast(str, self.config.get("id", "line")),
            support_streaming_message=False,
        )

    async def run(self) -> None:
        # 快速 disable/reload 时 terminate() 可能先于 run() 的函数体执行；此时不能再拉起
        # worker，否则它们没人取消，会一直挂在 queue.get() 上，还会用已关闭的 client 处理事件。
        if self.shutdown_event.is_set():
            return

        webhook_uuid = self.config.get("webhook_uuid")
        if webhook_uuid:
            log_webhook_info(f"{self.meta().id}(LINE)", webhook_uuid)
        else:
            logger.warning("[LINE] webhook_uuid 为空，统一 Webhook 可能无法接收消息。")

        self._workers = [
            asyncio.create_task(self._event_worker(index))
            for index in range(_EVENT_WORKER_COUNT)
        ]
        try:
            await self.shutdown_event.wait()
        finally:
            # run() 无论怎样退出都要收掉自己拉起的 worker，不留孤儿任务。
            try:
                await self._stop_workers()
            except asyncio.CancelledError:
                # run() 本身被取消时 await 会立刻中断，此时至少同步取消掉 worker；
                # 若 _workers 已被 terminate() 接手（列表为空），则由它负责收尾。
                for worker in self._workers:
                    worker.cancel()
                raise

    async def terminate(self) -> None:
        """停止受理新事件，给在飞事件留出时间，最后才关闭 HTTP client。

        顺序很重要：先让 worker 把能做完的做完，再关 session —— 否则在飞任务会撞上
        已关闭的 session，出现 session-closed 类错误。
        """
        self.shutdown_event.set()
        await self._stop_workers()
        await self.line_api.close()

    async def _stop_workers(self) -> None:
        """排空已受理事件后取消 worker。幂等，且可被 run() 与 terminate() 并发调用。"""
        async with self._workers_lock:
            if not self._workers:
                return
            workers, self._workers = self._workers, []
            try:
                await asyncio.wait_for(
                    self._inbound_queue.join(), timeout=_TERMINATE_TIMEOUT_SECONDS
                )
            except (TimeoutError, asyncio.TimeoutError):
                # 队列里排队的 + worker 已取出但没做完的，都属于「未处理完成」。
                logger.warning(
                    "[LINE] terminate timed out, %s accepted event(s) unprocessed.",
                    self._inbound_queue.qsize() + self._in_flight,
                )
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    # ------------------------------------------------------------ Webhook

    async def webhook_callback(self, request: Any) -> Any:
        raw_body = await request.get_data()
        signature = request.headers.get("x-line-signature")
        if not self.line_api.verify_signature(raw_body, signature):
            logger.warning("[LINE] invalid webhook signature")
            return "invalid signature", 400

        try:
            payload = await request.get_json(silent=False)
        except Exception as e:
            logger.warning("[LINE] invalid webhook body: %s", e)
            return "bad request", 400

        if not isinstance(payload, dict):
            return "bad request", 400

        if self.shutdown_event.is_set():
            return "shutting down", 503

        if not self.accept_webhook_payload(payload):
            # 503 不是可靠重投机制（LINE 的 redelivery 默认关闭且不保证送达），
            # 它是一个明确的过载信号，顺带在开了 redelivery 的部署里获得一次机会。
            logger.error(
                "[LINE] inbound queue is full (%s), event(s) rejected with 503.",
                _EVENT_QUEUE_MAXSIZE,
            )
            return "overloaded", 503

        return "ok", 200

    def accept_webhook_payload(self, payload: dict[str, Any]) -> bool:
        """把 payload 里的事件放进有界队列；任一条放不下时返回 False。

        去重登记必须发生在成功入队之后：入队失败会让整个 payload 收到 503，
        若此时已经登记，LINE 重投时这条事件会被当成重复事件跳过，从此永久丢失。
        同理，同一 payload 里已入队的事件会被登记，重投时正确跳过它们、只补做被拒的那些。
        """
        destination = str(payload.get("destination", "")).strip()
        if destination:
            self.destination = destination

        events = payload.get("events")
        if not isinstance(events, list):
            return True

        accepted = True
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("webhookEventId", ""))
            if event_id and self._is_event_seen(event_id):
                logger.debug("[LINE] duplicate event skipped: %s", event_id)
                continue
            try:
                self._inbound_queue.put_nowait(event)
            except asyncio.QueueFull:
                accepted = False
                continue
            if event_id:
                self._mark_event_seen(event_id)
        return accepted

    async def handle_webhook_event(self, payload: dict[str, Any]) -> None:
        """受理一个 webhook payload（不等待处理完成）。"""
        self.accept_webhook_payload(payload)

    async def _event_worker(self, index: int) -> None:
        while True:
            event = await self._inbound_queue.get()
            self._in_flight += 1
            try:
                await self._process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 已受理的事件处理失败必须留下完整堆栈，不得静默消失。
                logger.error(
                    "[LINE] worker %s failed to process event: %s",
                    index,
                    traceback.format_exc(),
                )
            finally:
                self._in_flight -= 1
                self._inbound_queue.task_done()

    async def _process_event(self, event: dict[str, Any]) -> None:
        abm = await self.convert_message(event)
        if abm is None:
            return
        await self.handle_msg(abm)

    # ------------------------------------------------------------ 入站转换

    async def convert_message(self, event: dict[str, Any]) -> AstrBotMessage | None:
        event_type = str(event.get("type", ""))
        if event_type not in {"message", "postback"}:
            logger.debug("[LINE] event type not handled: %s", event_type)
            return None
        if str(event.get("mode", "active")) == "standby":
            return None

        source = event.get("source", {})
        if not isinstance(source, dict):
            return None

        abm = AstrBotMessage()
        abm.self_id = self.destination or self.meta().id
        abm.message = []
        abm.raw_message = event

        source_type = str(source.get("type", ""))
        user_id = str(source.get("userId", "")).strip()
        group_id = str(source.get("groupId", "")).strip()
        room_id = str(source.get("roomId", "")).strip()

        if source_type in {"group", "room"}:
            abm.type = MessageType.GROUP_MESSAGE
            container_id = group_id or room_id
            abm.group = Group(group_id=container_id, group_name=container_id)
            abm.session_id = container_id
            sender_id = user_id or container_id
        elif source_type == "user":
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = user_id
            sender_id = user_id
        else:
            abm.type = MessageType.OTHER_MESSAGE
            abm.session_id = user_id or group_id or room_id or "unknown"
            sender_id = abm.session_id

        abm.timestamp = self._event_timestamp(event)
        abm.sender = MessageMember(
            user_id=sender_id,
            nickname=await self._resolve_nickname(
                source_type, sender_id, group_id, room_id
            ),
        )

        if event_type == "postback":
            return self._fill_postback_message(abm, event)
        return await self._fill_message_event(abm, event)

    @staticmethod
    def _event_timestamp(event: dict[str, Any]) -> int:
        event_timestamp = event.get("timestamp")
        if isinstance(event_timestamp, int):
            return (
                event_timestamp // 1000
                if event_timestamp > 1_000_000_000_000
                else event_timestamp
            )
        return int(time.time())

    def _fill_postback_message(
        self, abm: AstrBotMessage, event: dict[str, Any]
    ) -> AstrBotMessage:
        """postback 以结构化交互事件进入 pipeline，不转换成消息组件或文字命令。

        原始事件已经在 abm.raw_message 里，插件通过 event.is_postback() /
        get_postback_data() / get_postback_params() 读取。
        """
        abm.message_id = str(event.get("webhookEventId") or uuid.uuid4().hex)
        abm.message = []
        abm.message_str = ""
        return abm

    async def _fill_message_event(
        self, abm: AstrBotMessage, event: dict[str, Any]
    ) -> AstrBotMessage | None:
        message = event.get("message", {})
        if not isinstance(message, dict):
            return None

        abm.message_id = str(
            message.get("id")
            or event.get("webhookEventId")
            or event.get("deliveryContext", {}).get("deliveryId", "")
            or uuid.uuid4().hex
        )

        components = await self._parse_line_message_components(message)
        if not components:
            return None

        chat_id = abm.session_id
        # quoteToken 只能在取得它的那个聊天里使用，所以缓存键必须带聊天作用域。
        # 入站 Audio / File / Location 没有该字段，写入必须容忍缺失。
        self._quote_store.put_token(
            chat_id, abm.message_id, str(message.get("quoteToken") or "") or None
        )
        self._quote_store.put_content(chat_id, abm.message_id, components)

        quoted_message_id = str(message.get("quotedMessageId") or "").strip()
        if quoted_message_id:
            reply = await self._build_reply_component(chat_id, quoted_message_id)
            components = [reply, *components]

        abm.message = components
        abm.message_str = self._build_message_str(components)
        return abm

    async def _build_reply_component(self, chat_id: str, quoted_id: str) -> Reply:
        """尽力恢复被引用消息的内容：命中缓存或可回查则带内容，否则只留 ID。"""
        cached = self._quote_store.get_content(chat_id, quoted_id)
        if cached and not _local_media_present(cached):
            # 图片类组件引用的本地文件已被清理掉，重新下载
            logger.debug("[LINE] quoted %s cached media is gone, refetching", quoted_id)
            cached = None
        if cached:
            return Reply(
                id=quoted_id,
                chain=cached,
                message_str=self._build_message_str(cached),
            )

        # 未命中时不预先判断被引用消息的类型 —— 手上只有一个 ID，直接尝试回查。
        # 文本与贴纸 LINE 本就不提供回查，天然落到留空分支。
        content = await self.line_api.get_message_content(
            quoted_id, limit_bytes=QUOTE_LOOKUP_LIMIT, wait_transcoding=False
        )
        if content is None:
            logger.debug("[LINE] quoted message %s content unavailable", quoted_id)
            return Reply(id=quoted_id, chain=[], message_str="")

        component = await self._component_from_content(content)
        chain = [component] if component else []
        return Reply(
            id=quoted_id,
            chain=chain,
            message_str=self._build_message_str(chain),
        )

    @staticmethod
    async def _component_from_content(
        content: LineMessageContent,
    ) -> BaseMessageComponent | None:
        """按实际 MIME 把回查到的文件包装成组件。"""
        path = str(content.path)
        mime = await detect_file_mime_type_async(path)
        if mime.startswith("image/"):
            return Image.fromFileSystem(path)
        if mime.startswith("video/"):
            return Video(file=path, path=path)
        if mime.startswith("audio/"):
            return Record(file=path, url=path)
        return File(name=content.filename or Path(path).name, file=path, url=path)

    async def _parse_line_message_components(
        self,
        message: dict[str, Any],
    ) -> list:
        msg_type = str(message.get("type", ""))
        message_id = str(message.get("id", "")).strip()

        if msg_type == "text":
            text = str(message.get("text", ""))
            mention = message.get("mention")
            if isinstance(mention, dict):
                return self._parse_text_with_mentions(text, mention)
            return [Plain(text=text)] if text else []

        if msg_type == "image":
            component = await self._build_image_component(message_id, message)
            return [component] if component else [Plain(text="[image]")]

        if msg_type == "video":
            component = await self._build_video_component(message_id, message)
            return [component] if component else [Plain(text="[video]")]

        if msg_type == "audio":
            component = await self._build_audio_component(message_id, message)
            return [component] if component else [Plain(text="[audio]")]

        if msg_type == "file":
            component = await self._build_file_component(message_id, message)
            return [component] if component else [Plain(text="[file]")]

        if msg_type == "sticker":
            return [Plain(text="[sticker]")]

        return [Plain(text=f"[{msg_type}]")]

    def _parse_text_with_mentions(self, text: str, mention_obj: dict[str, Any]) -> list:
        """按 UTF-16 code unit 偏移切分带 mention 的文本。

        LINE 的 index / length 是 UTF-16 计量，Python str 按码点切片会在
        mention 之前含 emoji 时切出错误的目标。
        """
        mentions = mention_obj.get("mentionees", [])
        if not isinstance(mentions, list) or not mentions:
            return [Plain(text=text)] if text else []

        normalized = []
        for item in mentions:
            if not isinstance(item, dict):
                continue
            start = item.get("index")
            length = item.get("length")
            if not isinstance(start, int) or not isinstance(length, int):
                continue
            normalized.append((start, length, item))
        normalized.sort(key=lambda x: x[0])

        ret: list = []
        cursor = 0
        for start, length, item in normalized:
            if start > cursor:
                part = utf16_slice(text, cursor, start - cursor)
                if part:
                    ret.append(Plain(text=part))

            label = utf16_slice(text, start, length) or "@user"
            mention_type = str(item.get("type", ""))
            if mention_type == "all":
                # 用 AstrBot 标准的 AtAll，插件才能跨平台统一识别「@全体」。
                ret.append(AtAll())
            elif mention_type == "user":
                target_id = str(item.get("userId", "")).strip()
                ret.append(At(qq=target_id, name=label.lstrip("@")))
            else:
                ret.append(Plain(text=label))
            cursor = max(cursor, start + length)

        tail = utf16_split(text, cursor)[1]
        if tail:
            ret.append(Plain(text=tail))
        return ret

    # ------------------------------------------------------------ 入站媒体

    async def _build_image_component(
        self,
        message_id: str,
        message: dict[str, Any],
    ) -> Image | None:
        external_url = self._get_external_content_url(message)
        if external_url:
            return Image.fromURL(external_url)

        content = await self._download_inbound(message_id, ".jpg")
        if content is None:
            return None
        return Image.fromFileSystem(str(content.path))

    async def _build_video_component(
        self,
        message_id: str,
        message: dict[str, Any],
    ) -> Video | None:
        external_url = self._get_external_content_url(message)
        if external_url:
            return Video.fromURL(external_url)

        content = await self._download_inbound(message_id, ".mp4")
        if content is None:
            return None
        file_path = str(content.path)
        return Video(file=file_path, path=file_path)

    async def _build_audio_component(
        self,
        message_id: str,
        message: dict[str, Any],
    ) -> Record | None:
        source_ref = self._get_external_content_url(message)
        if not source_ref:
            content = await self._download_inbound(message_id, ".m4a")
            if content is None:
                return None
            source_ref = str(content.path)

        try:
            # 外链交给公共 MediaResolver 物化并转码；本地路径同一条路走完转码。
            path_wav = await MediaResolver(
                source_ref,
                media_type="audio",
                default_suffix=".wav",
                max_bytes=INBOUND_DOWNLOAD_LIMIT,
            ).to_path(target_format="wav")
        except Exception as e:
            logger.warning("[LINE] inbound audio unavailable: %s", e)
            return None
        return Record(file=path_wav, url=path_wav)

    async def _build_file_component(
        self,
        message_id: str,
        message: dict[str, Any],
    ) -> File | None:
        default_name = str(message.get("fileName", "")).strip() or f"{message_id}.bin"
        suffix = Path(default_name).suffix or ".bin"
        content = await self._download_inbound(message_id, suffix)
        if content is None:
            return None
        final_name = content.filename or default_name
        file_path = str(content.path)
        return File(name=final_name, file=file_path, url=file_path)

    async def _download_inbound(
        self, message_id: str, suffix: str
    ) -> LineMessageContent | None:
        """受限下载入站媒体；超限或失败返回 None，由调用方降级为占位组件。

        普通入站是最大的敞口 —— 它由用户而非 bot 触发，而 LINE 允许 200 MB 的视频。
        """
        if not message_id:
            return None
        return await self.line_api.get_message_content(
            message_id, limit_bytes=INBOUND_DOWNLOAD_LIMIT, suffix=suffix
        )

    @staticmethod
    def _get_external_content_url(message: dict[str, Any]) -> str:
        provider = message.get("contentProvider")
        if not isinstance(provider, dict):
            return ""
        if str(provider.get("type", "")) != "external":
            return ""
        return str(provider.get("originalContentUrl", "")).strip()

    # ------------------------------------------------------------ 昵称

    async def _resolve_nickname(
        self,
        source_type: str,
        user_id: str,
        group_id: str,
        room_id: str,
    ) -> str:
        """取真实显示名（私聊 / 群聊 / 多人聊天是三个不同 endpoint），带 TTL 缓存。"""
        fallback = user_id[:8]
        if not user_id:
            return fallback

        container = group_id or room_id or "user"
        cache_key = (container, user_id)
        now = time.time()
        cached = self._nickname_cache.get(cache_key)
        if cached is not None:
            if now - cached[0] < _NICKNAME_TTL_SECONDS:
                self._nickname_cache.move_to_end(cache_key)
                return cached[1]
            self._nickname_cache.pop(cache_key, None)

        if source_type == "group" and group_id:
            name = await self.line_api.get_group_member_display_name(group_id, user_id)
        elif source_type == "room" and room_id:
            name = await self.line_api.get_room_member_display_name(room_id, user_id)
        else:
            name = await self.line_api.get_user_display_name(user_id)

        if not name:
            return fallback
        self._nickname_cache[cache_key] = (now, name)
        _evict_profile_cache(self._nickname_cache, now)
        return name

    # ------------------------------------------------------------ 界面语言

    async def _resolve_language(self, user_id: str) -> str | None:
        """取用户的界面语言（BCP 47），带 TTL 缓存；取不到返回 None。

        与昵称共用一套缓存写法，但键只有 user_id —— language 是账号级属性，
        与所在群聊无关。取不到是常态（见 LineAPIClient.get_user_language），
        所以失败结果同样入缓存，否则群里非好友成员每条消息都要白打一次 API。
        """
        if not user_id:
            return None

        now = time.time()
        cached = self._language_cache.get(user_id)
        if cached is not None:
            if now - cached[0] < _NICKNAME_TTL_SECONDS:
                self._language_cache.move_to_end(user_id)
                return cached[1]
            self._language_cache.pop(user_id, None)

        language = await self.line_api.get_user_language(user_id)
        self._language_cache[user_id] = (now, language)
        _evict_profile_cache(self._language_cache, now)
        return language

    # ------------------------------------------------------------ 出站

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        """插件显式调用的主动消息接口 —— 唯一允许 push 的路径。"""
        batch = await build_line_batch(
            message_chain,
            self.config.get("image_host_chain") or None,
            allow_mentions=session.message_type == MessageType.GROUP_MESSAGE,
        )
        messages = finalize_line_messages(
            batch, quote_store=self._quote_store, chat_id=session.session_id
        )
        if messages:
            result = await self.line_api.push_message(session.session_id, messages)
            if result.ok:
                remember_sent_messages(
                    messages,
                    result,
                    quote_store=self._quote_store,
                    chat_id=session.session_id,
                )
        await super().send_by_session(session, message_chain)

    # ------------------------------------------------------------ 其它

    @staticmethod
    def _build_message_str(components: list) -> str:
        parts: list[str] = []
        for comp in components:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, At):
                parts.append(f"@{comp.name or comp.qq}")
            elif isinstance(comp, Image):
                parts.append("[image]")
            elif isinstance(comp, Video):
                parts.append("[video]")
            elif isinstance(comp, Record):
                parts.append("[audio]")
            elif isinstance(comp, File):
                parts.append(str(comp.name or "[file]"))
            elif isinstance(comp, Reply):
                continue
            else:
                parts.append(f"[{comp.type}]")
        return " ".join(i for i in parts if i).strip()

    def _clean_expired_events(self) -> None:
        current = time.time()
        expired = [
            event_id
            for event_id, ts in self._event_id_timestamps.items()
            if current - ts > 1800
        ]
        for event_id in expired:
            del self._event_id_timestamps[event_id]

    def _is_event_seen(self, event_id: str) -> bool:
        """该事件是否已被受理过（只查，不登记）。"""
        self._clean_expired_events()
        return event_id in self._event_id_timestamps

    def _mark_event_seen(self, event_id: str) -> None:
        """登记已成功入队的事件，用于之后的重投去重。"""
        self._event_id_timestamps[event_id] = time.time()

    def get_client(self) -> LineAPIClient:
        """获取平台的客户端对象。"""
        return self.line_api

    def create_event(self, message: AstrBotMessage) -> LineMessageEvent:
        """Creates a LINE message event.

        Args:
            message: AstrBot message object to wrap.

        Returns:
            Created LINE message event.
        """
        return LineMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            line_api=self.line_api,
            image_host_chain=self.config.get("image_host_chain") or None,
            quote_store=self._quote_store,
        )

    async def handle_msg(self, abm: AstrBotMessage) -> None:
        event = self.create_event(abm)

        # LINE 不在 webhook 事件里下发 locale，只能主动查 profile。放在这里而不是
        # create_event 里，是因为后者是同步的，且还被 star tool 的构造路径复用。
        language = await self._resolve_language(abm.sender.user_id)
        if language:
            event.set_extra("user_locale", language)

        if event.is_postback():
            # 未被插件处理的 postback 不触发默认 LLM（私聊同样成立）。真正的闸门是
            # process_stage 里的 not event.call_llm；预设 is_wake 之类的标志无效，
            # 且私聊分支会自行把 is_at_or_wake_command 置真。
            event.should_call_llm(True)
        self.commit_event(event)
