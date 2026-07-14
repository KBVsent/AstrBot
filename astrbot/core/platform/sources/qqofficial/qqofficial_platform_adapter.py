from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import botpy
import botpy.interaction
import botpy.message
from botpy import Client
from botpy.connection import ConnectionState
from botpy.gateway import BotWebSocket

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.media_utils import MediaResolver

from ...register import register_platform_adapter
from .qqofficial_message_event import QQOfficialMessageEvent

# remove logger handler
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)


def _parse_scene_refidx(data: dict[str, Any]) -> str | None:
    """从 message_scene.ext 中解析 REFIDX（msg_idx）"""
    scene = data.get("message_scene")
    if not isinstance(scene, dict):
        return None
    ext = scene.get("ext")
    if isinstance(ext, dict):
        val = ext.get("msg_idx")
        return str(val) if val else None
    if isinstance(ext, list):
        for item in ext:
            if isinstance(item, str) and item.startswith("msg_idx="):
                return item[len("msg_idx=") :] or None
    return None


def _set_raw_message_fields(message: Any, data: dict[str, Any]) -> None:
    """Preserve QQ message fields that qq-botpy does not expose.

    Args:
        message: Patched qq-botpy message object.
        data: Raw message payload from QQ.

    Returns:
        None.
    """
    if not isinstance(data, dict):
        data = {}
    message.raw_data = data
    message.message_type = data.get("message_type")
    msg_elements = data.get("msg_elements")
    message.msg_elements = msg_elements if isinstance(msg_elements, list) else []
    message.message_reference_id = _parse_scene_refidx(data)


class PatchedMessage(botpy.message.Message):
    __slots__ = ("raw_data", "message_type", "msg_elements", "message_reference_id")

    def __init__(
        self,
        api: Any,
        event_id: str | None,
        data: dict[str, Any],
    ) -> None:
        super().__init__(api, event_id, data)  # type: ignore
        _set_raw_message_fields(self, data)


class PatchedDirectMessage(botpy.message.DirectMessage):
    __slots__ = ("raw_data", "message_type", "msg_elements", "message_reference_id")

    def __init__(
        self,
        api: Any,
        event_id: str | None,
        data: dict[str, Any],
    ) -> None:
        super().__init__(api, event_id, data)  # type: ignore
        _set_raw_message_fields(self, data)


class PatchedC2CMessage(botpy.message.C2CMessage):
    __slots__ = ("raw_data", "message_type", "msg_elements", "message_reference_id")

    def __init__(
        self,
        api: Any,
        event_id: str | None,
        data: dict[str, Any],
    ) -> None:
        super().__init__(api, event_id, data)  # type: ignore
        _set_raw_message_fields(self, data)


class PatchedGroupMessage(botpy.message.GroupMessage):
    __slots__ = ("raw_data", "message_type", "msg_elements", "message_reference_id")

    def __init__(
        self,
        api: Any,
        event_id: str | None,
        data: dict[str, Any],
    ) -> None:
        super().__init__(api, event_id, data)  # type: ignore
        _set_raw_message_fields(self, data)

    class _User:
        def __init__(self, data: dict[str, Any]) -> None:
            self.id = data.get("id", None)
            self.username = data.get("username", None)
            self.bot = data.get("bot", None)
            self.avatar = data.get("avatar", None)
            self.member_openid = data.get("member_openid", None)
            self.user_openid = data.get("user_openid", None)
            self.is_you = data.get("is_you", None)
            self.scope = data.get("scope", None)

        def __repr__(self) -> str:
            return str(self.__dict__)


def _ensure_group_message_create_parser() -> None:
    """Register qq-botpy message parsers with QQ quote payload preservation."""

    def build_parser(event_name: str, message_cls: type) -> Any:
        """Build a ConnectionState parser for one QQ message event.

        Args:
            event_name: botpy dispatch event name.
            message_cls: Patched message class used to retain raw fields.

        Returns:
            Parser function bound by qq-botpy's ConnectionState.
        """

        def parse_message(self, payload: dict[str, Any]) -> None:
            qq_message = message_cls(
                self.api,
                payload.get("id", None),
                payload.get("d", {}),
            )
            self._dispatch(event_name, qq_message)

        return parse_message

    parser_specs = {
        "message_create": ("message_create", PatchedMessage),
        "at_message_create": ("at_message_create", PatchedMessage),
        "direct_message_create": ("direct_message_create", PatchedDirectMessage),
        "group_at_message_create": ("group_at_message_create", PatchedGroupMessage),
        "c2c_message_create": ("c2c_message_create", PatchedC2CMessage),
        "group_message_create": ("group_message_create", PatchedGroupMessage),
    }
    for parser_name, (event_name, message_cls) in parser_specs.items():
        setattr(
            ConnectionState,
            f"parse_{parser_name}",
            build_parser(event_name, message_cls),
        )


class ManagedBotWebSocket(BotWebSocket):
    def __init__(self, session, connection: Any, client: botClient):
        super().__init__(session, connection)
        self._client = client

    async def on_closed(self, close_status_code, close_msg):
        if self._client.is_shutting_down:
            logger.debug("[QQOfficial] Ignore websocket reconnect during shutdown.")
            return
        await super().on_closed(close_status_code, close_msg)

    async def close(self) -> None:
        self._can_reconnect = False
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()


# QQ 机器人官方框架
class botClient(Client):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._shutting_down = False
        self._active_websockets: set[ManagedBotWebSocket] = set()

    def set_platform(self, platform: QQOfficialPlatformAdapter) -> None:
        self.platform = platform

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down or self.is_closed()

    # 收到群消息
    async def on_group_at_message_create(
        self, message: botpy.message.GroupMessage
    ) -> None:
        abm = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            message,
            MessageType.GROUP_MESSAGE,
            force_group_mention=True,
        )
        abm.group_id = cast(str, message.group_openid)
        abm.session_id = abm.group_id
        self.platform.remember_session_scene(abm.session_id, "group")
        self._commit(abm)

    async def on_group_message_create(
        self, message: botpy.message.GroupMessage
    ) -> None:
        abm = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            message,
            MessageType.GROUP_MESSAGE,
        )
        abm.group_id = cast(str, message.group_openid)
        abm.session_id = abm.group_id
        self.platform.remember_session_scene(abm.session_id, "group")
        self._commit(abm)

    # 收到频道消息
    async def on_at_message_create(self, message: botpy.message.Message) -> None:
        abm = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            message,
            MessageType.GROUP_MESSAGE,
        )
        abm.group_id = message.channel_id
        abm.session_id = abm.group_id
        self.platform.remember_session_scene(abm.session_id, "channel")
        self._commit(abm)

    # 收到私聊消息
    async def on_direct_message_create(
        self, message: botpy.message.DirectMessage
    ) -> None:
        abm = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            message,
            MessageType.FRIEND_MESSAGE,
        )
        abm.session_id = abm.sender.user_id
        self.platform.remember_session_scene(abm.session_id, "friend")
        self._commit(abm)

    # 收到 C2C 消息
    async def on_c2c_message_create(self, message: botpy.message.C2CMessage) -> None:
        abm = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            message,
            MessageType.FRIEND_MESSAGE,
        )
        abm.session_id = abm.sender.user_id
        self.platform.remember_session_scene(abm.session_id, "friend")
        self._commit(abm)

    # 收到按钮点击回调
    async def on_interaction_create(
        self, interaction: botpy.interaction.Interaction
    ) -> None:
        abm = QQOfficialPlatformAdapter._parse_interaction_to_abm(interaction)
        if abm is None:
            logger.warning(
                f"[QQOfficial] 无法识别的 interaction chat_type: {interaction.chat_type}"
            )
            return
        scene = {0: "channel", 1: "group", 2: "friend"}.get(
            interaction.chat_type, "friend"
        )
        self.platform.remember_session_scene(abm.session_id, scene)
        # interaction 不是消息，不更新会话级 msg_id 缓存
        event = self._commit(abm, update_session_msg_id=False)
        asyncio.create_task(self._fallback_ack_interaction(event))

    async def _fallback_ack_interaction(self, event: QQOfficialMessageEvent) -> None:
        """等待下面任一条件即决定是否兜底：
        - 插件主动 ack：什么都不做（plugin 已发 PUT code N）
        - pipeline 处理完毕仍未 ack：发 PUT code 0（避免 QQ 客户端等待）
        - 0.5s 超时：发 PUT code 0 兜底
        """
        ack_task = asyncio.create_task(event._interaction_ack_done.wait())
        pipeline_task = asyncio.create_task(event._pipeline_finished.wait())
        try:
            done, pending = await asyncio.wait(
                {ack_task, pipeline_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.5,
            )
            for task in pending:
                task.cancel()
        except Exception as e:
            logger.warning(f"[QQOfficial] 等待 interaction ack 异常: {e}")
        if not event._interaction_acked:
            await event.ack_interaction(0)

    def _commit(
        self, abm: AstrBotMessage, update_session_msg_id: bool = True
    ) -> QQOfficialMessageEvent:
        if update_session_msg_id:
            self.platform.remember_session_message_id(abm.session_id, abm.message_id)
        event = self.platform.create_event(abm)
        self.platform.commit_event(event)
        return event

    async def bot_connect(self, session) -> None:
        logger.info("[QQOfficial] Websocket session starting.")

        websocket = ManagedBotWebSocket(session, self._connection, self)
        self._active_websockets.add(websocket)
        try:
            await websocket.ws_connect()
        except Exception as e:
            if not self.is_shutting_down:
                await websocket.on_error(e)
        finally:
            self._active_websockets.discard(websocket)

    async def shutdown(self) -> None:
        if self.is_shutting_down:
            return

        self._shutting_down = True
        await asyncio.gather(
            *(websocket.close() for websocket in list(self._active_websockets)),
            return_exceptions=True,
        )
        await self.close()


@register_platform_adapter("qq_official", "QQ 机器人官方 API 适配器")
class QQOfficialPlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        self.appid = platform_config["appid"]
        self.secret = platform_config["secret"]
        qq_group = platform_config["enable_group_c2c"]
        guild_dm = platform_config["enable_guild_direct_message"]

        if qq_group:
            self.intents = botpy.Intents(
                public_messages=True,
                public_guild_messages=True,
                direct_message=guild_dm,
                interaction=True,
            )
        else:
            self.intents = botpy.Intents(
                public_guild_messages=True,
                direct_message=guild_dm,
                interaction=True,
            )
        self.client = botClient(
            intents=self.intents,
            bot_log=False,
            timeout=20,
        )

        self.client.set_platform(self)

        _ensure_group_message_create_parser()

        self._session_last_message_id: dict[str, str] = {}
        self._session_scene: dict[str, str] = {}
        self._allow_group_proactive_send = True

        self.test_mode = os.environ.get("TEST_MODE", "off") == "on"

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        await self._send_by_session_common(session, message_chain)

    async def _send_by_session_common(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        message_chains = QQOfficialMessageEvent._split_message_chain_by_media(
            message_chain,
            inline_images=QQOfficialMessageEvent._should_inline_images(message_chain),
        )
        if len(message_chains) > 1:
            for split_message_chain in message_chains:
                await self._send_by_session_common(session, split_message_chain)
            return

        use_md = getattr(message_chain, "use_markdown_", None)
        has_keyboard = QQOfficialMessageEvent._has_keyboard(message_chain)
        if has_keyboard and use_md is False:
            use_md = True
        convert_img = has_keyboard and use_md is not False

        (
            plain_text,
            image_base64,
            image_path,
            record_file_path,
            video_file_source,
            file_source,
            file_name,
            keyboard_payload,
            reference_message_id,
        ) = await QQOfficialMessageEvent._parse_to_qqofficial(
            message_chain,
            convert_image_to_markdown=convert_img,
        )
        if (
            not plain_text
            and not image_path
            and not image_base64
            and not record_file_path
            and not video_file_source
            and not file_source
            and not keyboard_payload
        ):
            return

        # 主动推送不需要 msg_id，见 https://github.com/AstrBotDevs/AstrBot/issues/7904
        msg_id = self._session_last_message_id.get(session.session_id)
        scene = self._session_scene.get(session.session_id)
        allow_group_proactive_send = (
            session.message_type == MessageType.GROUP_MESSAGE
            and scene == "group"
            and getattr(self, "_allow_group_proactive_send", False)
        )
        if (
            not msg_id
            and session.message_type != MessageType.FRIEND_MESSAGE
            and not allow_group_proactive_send
        ):
            logger.warning(
                "[QQOfficial] No cached msg_id for session: %s, skip send_by_session",
                session.session_id,
            )
            return

        if keyboard_payload and not plain_text:
            plain_text = QQOfficialMessageEvent.EMPTY_MARKDOWN_PLACEHOLDER

        has_media = bool(
            image_base64 or record_file_path or video_file_source or file_source
        )
        # media 路径走 msg_type=7（纯媒体），其余走 content 或 markdown+keyboard
        if has_media:
            payload: dict[str, Any] = {"content": plain_text}
        elif use_md is False or not plain_text:
            payload = {"content": plain_text}
        else:
            from botpy.types.message import MarkdownPayload as _MD  # noqa: PLC0415

            payload = {
                "markdown": _MD(content=plain_text),
                "msg_type": 2,
            }
            if keyboard_payload:
                payload["keyboard"] = keyboard_payload
        # 主动发送（无缓存 msg_id 的群消息）时不携带 msg_id
        if msg_id and not allow_group_proactive_send:
            payload["msg_id"] = msg_id
        # 引用回复：主动推送无触发消息，调用方需自行提供正确的引用 ID
        # （群/C2C 为 REFIDX，频道为 message_id）。文本 / 图片+文字可引用；
        # markdown(msg_type=2) 及语音/视频/文件/keyboard 的 payload 不接受引用。
        # 主动路径下 markdown 仅在无媒体时出现，因此按 payload 是否含 markdown 判断即可。
        has_ref_blocking_media = bool(
            record_file_path or video_file_source or file_source
        )
        is_markdown_payload = "markdown" in payload
        if (
            reference_message_id
            and not has_ref_blocking_media
            and not keyboard_payload
            and not is_markdown_payload
        ):
            from botpy.types.message import Reference as _Ref  # noqa: PLC0415

            payload["message_reference"] = _Ref(
                message_id=reference_message_id,
                ignore_get_message_error=True,
            )
        # 媒体 + keyboard 时，稍后需要补发一条 markdown+keyboard
        need_keyboard_followup = has_media and keyboard_payload is not None
        ret: Any = None
        send_helper = SimpleNamespace(bot=self.client)

        if session.message_type == MessageType.GROUP_MESSAGE:
            if scene == "group":
                payload["msg_seq"] = random.randint(1, 10000)
                if image_base64:
                    media = await QQOfficialMessageEvent.upload_group_and_c2c_image(
                        send_helper,  # type: ignore
                        image_base64,
                        QQOfficialMessageEvent.IMAGE_FILE_TYPE,
                        group_openid=session.session_id,
                    )
                    payload["media"] = media
                    payload["msg_type"] = 7
                if record_file_path:
                    media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                        send_helper,  # type: ignore
                        record_file_path,
                        QQOfficialMessageEvent.VOICE_FILE_TYPE,
                        group_openid=session.session_id,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                if video_file_source:
                    media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                        send_helper,  # type: ignore
                        video_file_source,
                        QQOfficialMessageEvent.VIDEO_FILE_TYPE,
                        group_openid=session.session_id,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("msg_id", None)
                if file_source:
                    media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                        send_helper,  # type: ignore
                        file_source,
                        QQOfficialMessageEvent.FILE_FILE_TYPE,
                        file_name=file_name,
                        group_openid=session.session_id,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("msg_id", None)
                ret = await self.client.api.post_group_message(
                    group_openid=session.session_id,
                    **payload,
                )
            else:
                if image_path:
                    payload["file_image"] = image_path
                ret = await self.client.api.post_message(
                    channel_id=session.session_id,
                    **payload,
                )

        elif session.message_type == MessageType.FRIEND_MESSAGE:
            # 参考 https://bot.q.qq.com/wiki/develop/pythonsdk/api/message/post_message.html
            # msg_id 缺失时认为是主动推送，而似乎至少在私聊上主动推送是没有被限制的，这里直接移除 msg_id 可以避免越权或 msg_id 不可用的bug
            payload.pop("msg_id", None)
            payload["msg_seq"] = random.randint(1, 10000)
            if image_base64:
                media = await QQOfficialMessageEvent.upload_group_and_c2c_image(
                    send_helper,  # type: ignore
                    image_base64,
                    QQOfficialMessageEvent.IMAGE_FILE_TYPE,
                    openid=session.session_id,
                )
                payload["media"] = media
                payload["msg_type"] = 7
            if record_file_path:
                media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                    send_helper,  # type: ignore
                    record_file_path,
                    QQOfficialMessageEvent.VOICE_FILE_TYPE,
                    openid=session.session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7
            if video_file_source:
                media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                    send_helper,  # type: ignore
                    video_file_source,
                    QQOfficialMessageEvent.VIDEO_FILE_TYPE,
                    openid=session.session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7
            if file_source:
                media = await QQOfficialMessageEvent.upload_group_and_c2c_media(
                    send_helper,  # type: ignore
                    file_source,
                    QQOfficialMessageEvent.FILE_FILE_TYPE,
                    file_name=file_name,
                    openid=session.session_id,
                )
                if media:
                    payload["media"] = media
                    payload["msg_type"] = 7

            ret = await QQOfficialMessageEvent.post_c2c_message(
                send_helper,  # type: ignore
                openid=session.session_id,
                **payload,
            )
        else:
            logger.warning(
                "[QQOfficial] Unsupported message type for send_by_session: %s",
                session.message_type,
            )
            return

        sent_message_id = self._extract_message_id(ret)
        if sent_message_id:
            self.remember_session_message_id(session.session_id, sent_message_id)

        # 媒体抢占 msg_type=7 后补发 markdown+keyboard
        if need_keyboard_followup and keyboard_payload:
            from botpy.types.message import MarkdownPayload as _MD  # noqa: PLC0415

            followup: dict[str, Any] = {
                "markdown": _MD(content=plain_text),
                "msg_type": 2,
                "msg_id": msg_id,
                "keyboard": keyboard_payload,
                "msg_seq": random.randint(1, 10000),
            }
            try:
                if session.message_type == MessageType.GROUP_MESSAGE:
                    scene = self._session_scene.get(session.session_id)
                    if scene == "group":
                        await self.client.api.post_group_message(
                            group_openid=session.session_id,
                            **followup,
                        )
                elif session.message_type == MessageType.FRIEND_MESSAGE:
                    followup.pop("msg_id", None)
                    await QQOfficialMessageEvent.post_c2c_message(
                        send_helper,  # type: ignore
                        openid=session.session_id,
                        **followup,
                    )
            except Exception as e:
                logger.warning(f"[QQOfficial] keyboard 补发失败: {e}")

        await Platform.send_by_session(self, session, message_chain)

    def remember_session_message_id(self, session_id: str, message_id: str) -> None:
        if not session_id or not message_id:
            return
        self._session_last_message_id[session_id] = message_id

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        if not session_id or not scene:
            return
        self._session_scene[session_id] = scene

    def _extract_message_id(self, ret: Any) -> str | None:
        if isinstance(ret, dict):
            message_id = ret.get("id")
            return str(message_id) if message_id else None
        message_id = getattr(ret, "id", None)
        if message_id:
            return str(message_id)
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="qq_official",
            description="QQ 机器人官方 API 适配器",
            id=cast(str, self.config.get("id")),
            support_proactive_message=True,
        )

    def create_event(self, message: AstrBotMessage) -> QQOfficialMessageEvent:
        """Creates a QQ Official message event.

        Args:
            message: AstrBot message object to wrap.

        Returns:
            Created QQ Official message event.
        """
        return QQOfficialMessageEvent(
            message.message_str,
            message,
            self.meta(),
            message.session_id,
            self.client,
        )

    @staticmethod
    def _normalize_attachment_url(url: str | None) -> str:
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"https://{url}"

    @staticmethod
    async def _prepare_audio_attachment(
        url: str,
        filename: str,
    ) -> Record:
        ext = Path(filename).suffix.lower()
        source_ext = ext or ".audio"
        path_wav = await MediaResolver(
            url,
            media_type="audio",
            default_suffix=source_ext,
        ).to_path(target_format="wav")

        return Record(file=path_wav, url=path_wav)

    @staticmethod
    async def _append_attachments(
        msg: list[BaseMessageComponent],
        attachments: list | None,
    ) -> None:
        if not attachments:
            return

        for attachment in attachments:
            if isinstance(attachment, dict):
                content_type = str(
                    attachment.get("content_type")
                    or attachment.get("contentType")
                    or "",
                ).lower()
                url = QQOfficialPlatformAdapter._normalize_attachment_url(
                    cast(str | None, attachment.get("url"))
                )
                filename = cast(
                    str,
                    attachment.get("filename")
                    or attachment.get("name")
                    or "attachment",
                )
            else:
                content_type = cast(
                    str,
                    getattr(attachment, "content_type", "") or "",
                ).lower()
                url = QQOfficialPlatformAdapter._normalize_attachment_url(
                    cast(str | None, getattr(attachment, "url", None))
                )
                filename = cast(
                    str,
                    getattr(attachment, "filename", None)
                    or getattr(attachment, "name", None)
                    or "attachment",
                )
            if not url:
                continue

            if content_type.startswith("image"):
                msg.append(Image.fromURL(url))
            else:
                ext = Path(filename).suffix.lower()
                image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
                audio_exts = {
                    ".mp3",
                    ".wav",
                    ".ogg",
                    ".m4a",
                    ".amr",
                    ".silk",
                }
                video_exts = {
                    ".mp4",
                    ".mov",
                    ".avi",
                    ".mkv",
                    ".webm",
                }

                if content_type.startswith("voice") or ext in audio_exts:
                    try:
                        msg.append(
                            await QQOfficialPlatformAdapter._prepare_audio_attachment(
                                url,
                                filename,
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            "[QQOfficial] Failed to prepare audio attachment %s: %s",
                            url,
                            e,
                        )
                        msg.append(Record.fromURL(url))
                elif content_type.startswith("video") or ext in video_exts:
                    msg.append(Video.fromURL(url))
                elif content_type.startswith("image") or ext in image_exts:
                    msg.append(Image.fromURL(url))
                else:
                    msg.append(File(name=filename, file=url, url=url))

    @staticmethod
    def _parse_face_message(content: str) -> str:
        """Parse QQ official face message format and convert to readable text.

        QQ official face message format:
        <faceType=4,faceId="",ext="eyJ0ZXh0IjoiW+a7oeWktOmXruWPt10ifQ==">

        The ext field contains base64-encoded JSON with a 'text' field
        describing the emoji (e.g., '[满头问号]').

        Args:
            content: The message content that may contain face tags.

        Returns:
            Content with face tags replaced by readable emoji descriptions.
        """
        import base64
        import json
        import re

        def replace_face(match):
            face_tag = match.group(0)
            # Extract ext field from the face tag
            ext_match = re.search(r'ext="([^"]*)"', face_tag)
            if ext_match:
                try:
                    ext_encoded = ext_match.group(1)
                    # Decode base64 and parse JSON
                    ext_decoded = base64.b64decode(ext_encoded).decode("utf-8")
                    ext_data = json.loads(ext_decoded)
                    emoji_text = ext_data.get("text", "")
                    if emoji_text:
                        return f"[表情:{emoji_text}]"
                except Exception:
                    pass
            # Fallback if parsing fails
            return "[表情]"

        # Match face tags: <faceType=...>
        return re.sub(r"<faceType=\d+[^>]*>", replace_face, content)

    @staticmethod
    async def _parse_from_qqofficial(
        message: botpy.message.Message
        | botpy.message.GroupMessage
        | botpy.message.DirectMessage
        | botpy.message.C2CMessage,
        message_type: MessageType,
        force_group_mention: bool = False,
    ) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.type = message_type
        abm.timestamp = int(time.time())
        abm.raw_message = message
        abm.message_id = message.id
        # REFIDX（message_scene.ext.msg_idx），用于群/C2C 的引用回复。
        # 插件可用 Reply(id=event.message_obj.message_id) 引用当前消息，适配器会
        # 在发送时自动翻译为 REFIDX；也可直接读取本字段拿到原始 REFIDX。
        abm.message_reference_id = getattr(message, "message_reference_id", None)
        # abm.tag = "qq_official"
        msg: list[BaseMessageComponent] = []
        message_reference = getattr(message, "message_reference", None)
        quoted_message_id = getattr(message_reference, "message_id", None)
        raw_message_type = getattr(message, "message_type", None)
        try:
            is_quoted_message = int(raw_message_type or 0) == 103
        except (TypeError, ValueError):
            is_quoted_message = False
        msg_elements = getattr(message, "msg_elements", None)
        quoted_message_str = ""
        quoted_element_message_id = ""
        quoted_chain: list[BaseMessageComponent] = []
        if is_quoted_message and isinstance(msg_elements, list) and msg_elements:
            quoted_element = msg_elements[0]
            if isinstance(quoted_element, dict):
                quoted_content = quoted_element.get("content")
                quoted_attachments = quoted_element.get("attachments")
                quoted_element_message_id = str(
                    quoted_element.get("id") or quoted_element.get("message_id") or "",
                )
            else:
                quoted_content = getattr(quoted_element, "content", None)
                quoted_attachments = getattr(quoted_element, "attachments", None)
                quoted_element_message_id = str(
                    getattr(quoted_element, "id", None)
                    or getattr(quoted_element, "message_id", None)
                    or "",
                )

            quoted_message_str = QQOfficialPlatformAdapter._parse_face_message(
                str(quoted_content or "").strip()
            )
            if quoted_message_str:
                quoted_chain.append(Plain(quoted_message_str))
            if isinstance(quoted_attachments, list):
                await QQOfficialPlatformAdapter._append_attachments(
                    quoted_chain,
                    quoted_attachments,
                )
        if quoted_message_id or quoted_element_message_id or quoted_chain:
            msg.append(
                Reply(
                    id=str(quoted_message_id or quoted_element_message_id or ""),
                    chain=quoted_chain,
                    message_str=quoted_message_str,
                    text=quoted_message_str,
                )
            )

        if isinstance(message, botpy.message.GroupMessage) or isinstance(
            message,
            botpy.message.C2CMessage,
        ):
            if isinstance(message, botpy.message.GroupMessage):
                abm.sender = MessageMember(
                    message.author.member_openid,
                    getattr(message.author, "username", "") or "",
                )
                abm.group_id = message.group_openid
                all_mentions = list(getattr(message, "mentions", None) or [])
                bot_mentions = [
                    mention
                    for mention in all_mentions
                    if getattr(mention, "is_you", False) is True
                    and getattr(mention, "id", None) is not None
                ]
                bot_mention_ids = [str(mention.id) for mention in bot_mentions]
                group_mentioned = bool(bot_mention_ids) or force_group_mention
                abm.self_id = bot_mention_ids[0] if bot_mention_ids else "qq_official"
                # 构造 mention_id -> At 组件映射；@全体成员单独收集（正文中无 <@id> 标记）。
                mention_at_map: dict[str, BaseMessageComponent] = {}
                all_member_ats: list[BaseMessageComponent] = []
                for mention in all_mentions:
                    if getattr(mention, "scope", None) == "all":
                        all_member_ats.append(At(qq="all", name="全体成员"))
                        continue
                    mid = getattr(mention, "id", None) or getattr(
                        mention, "member_openid", None
                    )
                    if not mid:
                        continue
                    mid = str(mid)
                    if getattr(mention, "is_you", False) is True:
                        mention_at_map[mid] = At(
                            qq=abm.self_id,
                            name=getattr(mention, "username", "") or "",
                        )
                    else:
                        mention_at_map[mid] = At(
                            qq=str(getattr(mention, "member_openid", None) or mid),
                            name=getattr(mention, "username", "") or "",
                        )
                # 按 <@id> 在正文中的真实位置交错构造 At / Plain 段，保留 @ 的原始顺序，
                content = message.content or ""
                ordered: list[BaseMessageComponent] = []
                bot_at_inserted = False
                last_idx = 0
                for m in re.finditer(r"<@!?([^>]+)>", content):
                    text_seg = content[last_idx : m.start()]
                    last_idx = m.end()
                    if text_seg:
                        ordered.append(
                            Plain(
                                QQOfficialPlatformAdapter._parse_face_message(text_seg)
                            )
                        )
                    at_comp = mention_at_map.get(m.group(1))
                    if at_comp is not None:
                        ordered.append(at_comp)
                        if str(getattr(at_comp, "qq", "")) == str(abm.self_id):
                            bot_at_inserted = True
                tail_seg = content[last_idx:]
                if tail_seg:
                    ordered.append(
                        Plain(QQOfficialPlatformAdapter._parse_face_message(tail_seg))
                    )
                # message_str 保留全部正文（剥离所有 @ 标记后）
                plain_content_raw = content
                for mention_id in mention_at_map:
                    plain_content_raw = plain_content_raw.replace(
                        f"<@{mention_id}>", ""
                    ).replace(f"<@!{mention_id}>", "")
                abm.message_str = QQOfficialPlatformAdapter._parse_face_message(
                    plain_content_raw.strip()
                )
                # 被动 @（force_group_mention）时正文可能没有 <@bot> 标记，补一个首段 At(bot)
                if group_mentioned and not bot_at_inserted:
                    mention_name = (
                        getattr(bot_mentions[0], "username", "") if bot_mentions else ""
                    )
                    ordered.insert(0, At(qq=abm.self_id, name=mention_name))
                # @全体成员无位置信息，统一放在最前（唤醒守卫允许 qq == "all"）
                msg.extend(all_member_ats)
                msg.extend(ordered)
            else:
                abm.sender = MessageMember(
                    message.author.user_openid,
                    getattr(message.author, "username", "") or "",
                )
                abm.message_str = QQOfficialPlatformAdapter._parse_face_message(
                    (message.content or "").strip()
                )
                abm.self_id = "unknown_selfid"
                msg.append(At(qq="qq_official"))
                msg.append(Plain(abm.message_str))
            await QQOfficialPlatformAdapter._append_attachments(
                msg, message.attachments
            )
            abm.message = msg

        elif isinstance(message, botpy.message.Message) or isinstance(
            message,
            botpy.message.DirectMessage,
        ):
            if isinstance(message, botpy.message.Message):
                abm.self_id = str(message.mentions[0].id)
            else:
                abm.self_id = ""

            plain_content = QQOfficialPlatformAdapter._parse_face_message(
                message.content.replace(
                    "<@!" + str(abm.self_id) + ">",
                    "",
                ).strip()
            )

            await QQOfficialPlatformAdapter._append_attachments(
                msg, message.attachments
            )
            abm.message = msg
            abm.message_str = plain_content
            abm.sender = MessageMember(
                str(message.author.id),
                str(message.author.username),
            )
            msg.append(At(qq="qq_official"))
            msg.append(Plain(plain_content))

            if isinstance(message, botpy.message.Message):
                abm.group_id = message.channel_id
        else:
            raise ValueError(f"Unknown message type: {message_type}")
        if not abm.self_id:
            abm.self_id = "qq_official"
        return abm

    @staticmethod
    def _parse_interaction_to_abm(
        interaction: botpy.interaction.Interaction,
    ) -> AstrBotMessage | None:
        """将 QQ 按钮交互事件包装成 AstrBotMessage。

        chat_type: 0=频道 / 1=群 / 2=C2C

        message_id 取 ``interaction.event_id``（外层派发事件 id)
        """
        abm = AstrBotMessage()
        abm.timestamp = int(time.time())
        abm.raw_message = interaction
        abm.message_id = interaction.event_id or ""
        abm.self_id = "qq_official"
        abm.message_str = ""
        abm.message = []

        resolved = interaction.data.resolved if interaction.data else None
        button_id = getattr(resolved, "button_id", None) if resolved else None
        user_id_in_resolved = getattr(resolved, "user_id", None) if resolved else None

        chat_type = interaction.chat_type
        if chat_type == 0:
            # 频道
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(interaction.channel_id or interaction.guild_id or "")
            abm.session_id = abm.group_id
            abm.sender = MessageMember(user_id_in_resolved or "", "")
        elif chat_type == 1:
            # 群
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = interaction.group_openid or ""
            abm.session_id = abm.group_id
            abm.sender = MessageMember(
                interaction.group_member_openid or user_id_in_resolved or "", ""
            )
        elif chat_type == 2:
            # C2C
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = interaction.user_openid or ""
            abm.sender = MessageMember(abm.session_id, "")
        else:
            return None

        logger.debug(
            f"[QQOfficial] interaction_create chat_type={chat_type} "
            f"button_id={button_id} session={abm.session_id}"
        )
        return abm

    def run(self):
        return self.client.start(appid=self.appid, secret=self.secret)

    def get_client(self) -> botClient:
        return self.client

    async def terminate(self) -> None:
        await self.client.shutdown()
        logger.info("QQ 官方机器人接口 适配器已被关闭")
