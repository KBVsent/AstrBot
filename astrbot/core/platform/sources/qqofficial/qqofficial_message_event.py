import asyncio
import base64
import logging
import os
import random
from typing import cast

import aiofiles
import botpy
import botpy.errors
import botpy.interaction
import botpy.message
import botpy.types
import botpy.types.message
from botpy import Client
from botpy.http import Route
from botpy.types import message
from botpy.types.message import MarkdownPayload, Media, Reference
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Reply, Video
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core.utils.media_utils import MediaResolver, file_uri_to_path, is_file_uri

from ._markdown_media import image_to_markdown_fragment
from .components import QQCButton, QQCKeyboard


class APIReturnNoneError(Exception):
    pass


def _patch_qq_botpy_formdata() -> None:
    """Patch qq-botpy for aiohttp>=3.12 compatibility.

    qq-botpy 1.2.1 defines botpy.http._FormData._gen_form_data() and expects
    aiohttp.FormData to have a private flag named _is_processed, which is no
    longer present in newer aiohttp versions.
    """

    try:
        from botpy.http import _FormData  # type: ignore

        if not hasattr(_FormData, "_is_processed"):
            setattr(_FormData, "_is_processed", False)
    except Exception:
        logger.debug("[QQOfficial] Skip botpy FormData patch.")


_patch_qq_botpy_formdata()


def _qqofficial_retry(max_attempts: int = 5):
    """Retry decorator for QQ Official API transient errors (HTTP 500/504)"""
    return retry(
        retry=retry_if_exception_type(
            (
                botpy.errors.ServerError,
                botpy.errors.SequenceNumberError,
                OSError,
                asyncio.TimeoutError,
                APIReturnNoneError,
            )
        ),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


_QQOFFICIAL_SEND_API_ERRORS = (
    botpy.errors.ForbiddenError,
    botpy.errors.MethodNotAllowedError,
    botpy.errors.NotFoundError,
    botpy.errors.SequenceNumberError,
    botpy.errors.ServerError,
)


class QQOfficialMessageEvent(AstrMessageEvent):
    IMAGE_FILE_TYPE = 1
    VIDEO_FILE_TYPE = 2
    VOICE_FILE_TYPE = 3
    FILE_FILE_TYPE = 4
    STREAM_MARKDOWN_NEWLINE_ERROR = "流式消息md分片需要\\n结束"
    # 没有正文但带 keyboard 时的占位（QQ markdown content 不可为空）
    EMPTY_MARKDOWN_PLACEHOLDER = "​"

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        bot: Client,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.bot = bot
        self.send_buffer = None
        # bot 自己最后一条成功发出的消息 id，供 recall() 默认撤回使用
        self.last_sent_message_id: str | None = None
        self._interaction_acked = False
        self._interaction_ack_done = asyncio.Event()
        self._interaction_ack_code: int = 0

    async def ack_interaction(self, code: int = 0) -> None:
        """向 QQ 官方上报按钮交互结果。

        code: 0=成功, 1=操作失败, 2=操作频繁, 3=重复操作, 4=没有权限, 5=仅管理员。

        每个 interaction 只会真正上报一次，重复调用会被忽略。
        非 interaction 事件调用本方法是 no-op。
        """
        if self._interaction_acked:
            logger.debug(f"[QQOfficial] ack_interaction 跳过(已 ack)，请求 code={code}")
            return
        interaction = self.message_obj.raw_message
        if not isinstance(interaction, botpy.interaction.Interaction):
            return
        self._interaction_acked = True
        self._interaction_ack_code = code
        logger.debug(
            f"[QQOfficial] ack_interaction 发送 code={code} id={interaction.id}"
        )
        try:
            await self.bot.api.on_interaction_result(interaction.id, code)
        except Exception as e:
            logger.warning(f"[QQOfficial] interaction ack 失败: {e}")
        finally:
            self._interaction_ack_done.set()

    async def send(self, message: MessageChain) -> None:
        self.send_buffer = message
        ret = await self._post_send()
        sent_id = self._extract_response_message_id(ret)
        if sent_id is not None:
            self.last_sent_message_id = sent_id

    async def recall(self, message_id: str | None = None, hidetip: bool = False) -> bool:
        """撤回消息。

        Args:
            message_id: 要撤回的消息 id。为空时撤回 bot 自己最后发出的那条消息
            hidetip: 频道场景是否隐藏"消息已撤回"小灰条，群/C2C 场景忽略

        Returns:
            是否撤回成功
        """
        source = self.message_obj.raw_message
        mid = message_id or self.last_sent_message_id
        if not mid:
            logger.warning("[QQOfficial] recall 无可撤回的 message_id")
            return False

        http = self.bot.api._http
        try:
            match source:
                case botpy.message.GroupMessage():
                    if not source.group_openid:
                        return False
                    await http.request(
                        Route(
                            "DELETE",
                            "/v2/groups/{group_openid}/messages/{message_id}",
                            group_openid=source.group_openid,
                            message_id=mid,
                        )
                    )
                case botpy.message.C2CMessage():
                    await http.request(
                        Route(
                            "DELETE",
                            "/v2/users/{user_openid}/messages/{message_id}",
                            user_openid=source.author.user_openid,
                            message_id=mid,
                        )
                    )
                case botpy.message.Message():
                    await self.bot.api.recall_message(
                        channel_id=source.channel_id,
                        message_id=mid,
                        hidetip=hidetip,
                    )
                case botpy.message.DirectMessage():
                    await http.request(
                        Route(
                            "DELETE",
                            "/dms/{guild_id}/messages/{message_id}",
                            guild_id=source.guild_id,
                            message_id=mid,
                        ),
                        params={"hidetip": str(hidetip).lower()},
                    )
                case _:
                    logger.debug(
                        "[QQOfficial] recall 不支持的消息源类型: %s", type(source)
                    )
                    return False
            return True
        except Exception as e:
            logger.debug(f"[QQOfficial] 撤回失败 message_id={mid}: {e}")
            return False

    async def send_streaming(self, generator, use_fallback: bool = False):
        """流式输出仅支持消息列表私聊（C2C），其他消息源退化为普通发送"""
        # 先标记事件层“已执行发送操作”，避免异常路径遗漏
        await super().send_streaming(generator, use_fallback)
        # QQ C2C 流式协议：开始/中间分片使用 state=1，结束分片使用 state=10
        stream_payload = {"state": 1, "id": None, "index": 0, "reset": False}
        last_edit_time = 0  # 上次发送分片的时间
        throttle_interval = 1  # 分片间最短间隔 (秒)
        ret = None
        source = (
            self.message_obj.raw_message
        )  # 提前获取，避免 generator 为空时 NameError
        try:
            async for chain in generator:
                source = self.message_obj.raw_message

                if not isinstance(source, botpy.message.C2CMessage):
                    # 非 C2C 场景：直接累积，最后统一发
                    if not self.send_buffer:
                        self.send_buffer = chain
                    else:
                        self.send_buffer.chain.extend(chain.chain)
                    continue

                # ---- C2C 流式场景 ----

                # tool_call break 信号：工具开始执行，先把已有 buffer 以 state=10 结束当前流式段
                if chain.type == "break":
                    if self.send_buffer:
                        stream_payload["state"] = 10
                        ret = await self._post_send(stream=stream_payload)
                        ret_id = self._extract_response_message_id(ret)
                        if ret_id is not None:
                            stream_payload["id"] = ret_id
                    # 重置 stream_payload，为下一段流式做准备
                    stream_payload = {
                        "state": 1,
                        "id": None,
                        "index": 0,
                        "reset": False,
                    }
                    last_edit_time = 0
                    continue

                # 累积内容
                if not self.send_buffer:
                    self.send_buffer = chain
                else:
                    self.send_buffer.chain.extend(chain.chain)

                # 节流：按时间间隔发送中间分片
                current_time = asyncio.get_running_loop().time()
                if current_time - last_edit_time >= throttle_interval:
                    ret = cast(
                        message.Message,
                        await self._post_send(stream=stream_payload),
                    )
                    stream_payload["index"] += 1
                    ret_id = self._extract_response_message_id(ret)
                    if ret_id is not None:
                        stream_payload["id"] = ret_id
                    last_edit_time = asyncio.get_running_loop().time()
                    self.send_buffer = None  # 清空已发送的分片，避免下次重复发送旧内容

            if isinstance(source, botpy.message.C2CMessage):
                # 结束流式对话，发送 buffer 中剩余内容
                stream_payload["state"] = 10
                ret = await self._post_send(stream=stream_payload)
            else:
                ret = await self._post_send()

        except Exception as e:
            logger.error(f"发送流式消息时出错: {e}", exc_info=True)
            # 避免累计内容在异常后被整包重复发送：仅清理缓存，不做非流式整包兜底
            # 如需兜底，应该只发送未发送 delta（后续可继续优化）
            self.send_buffer = None

        return None

    @staticmethod
    def _extract_response_message_id(ret) -> str | None:
        """兼容 qq-botpy 返回 Message 对象或 dict 两种形态。"""
        if ret is None:
            return None
        if isinstance(ret, dict):
            ret_id = ret.get("id")
            return str(ret_id) if ret_id is not None else None
        ret_id = getattr(ret, "id", None)
        return str(ret_id) if ret_id is not None else None

    @staticmethod
    def _resolve_reference_id(source, reply_id: str) -> str | None:
        """把消息链里 Reply.id 解析成 QQ 引用回复真正需要的 message_id。

        - 群/C2C：QQ 只认 REFIDX（message_scene.ext.msg_idx），不认 ROBOT1.0_xxx
          形式的 message_id。若调用方传的已是 REFIDX（前缀 ``REFIDX_``）则直接用；
          否则视作「引用当前触发消息」，取该消息解析出的 REFIDX。
        - 频道（Message/DirectMessage）：直接使用真实 message_id。
        """
        if isinstance(source, (botpy.message.GroupMessage, botpy.message.C2CMessage)):
            if reply_id.startswith("REFIDX_"):
                return reply_id
            return getattr(source, "message_reference_id", None)
        return reply_id

    @staticmethod
    def _has_keyboard(message: MessageChain) -> bool:
        return any(isinstance(seg, (QQCKeyboard, QQCButton)) for seg in message.chain)

    @classmethod
    def _should_inline_images(cls, message: MessageChain) -> bool:
        """带 keyboard 时强制 markdown，图片会被转成 markdown 内联语法，
        因此不应被当作需要拆分的媒体。但若链中还有二进制媒体（语音/视频/文件），
        这些仍需独立成条，此时退回常规拆分以保持「每条至多一个二进制媒体」不变式。"""
        if not cls._has_keyboard(message):
            return False
        has_binary_media = any(
            isinstance(seg, Record | Video | File) for seg in message.chain
        )
        return not has_binary_media

    @staticmethod
    def _split_message_chain_by_media(
        message: MessageChain, inline_images: bool = False
    ) -> list[MessageChain]:
        chunks: list[MessageChain] = []
        current_chain = []
        current_has_media = False

        for component in message.chain:
            is_media = isinstance(component, Image | Record | Video | File)
            # 图片将被内联进 markdown（与 keyboard 共存），不触发拆分
            if inline_images and isinstance(component, Image):
                is_media = False
            if is_media and current_has_media:
                chunks.append(
                    MessageChain(
                        chain=current_chain,
                        use_t2i_=message.use_t2i_,
                        type=message.type,
                    )
                )
                current_chain = []
                current_has_media = False

            current_chain.append(component)
            current_has_media = current_has_media or is_media

        if current_chain or not message.chain:
            chunks.append(
                MessageChain(
                    chain=current_chain,
                    use_t2i_=message.use_t2i_,
                    type=message.type,
                )
            )

        return chunks

    async def _post_send(self, stream: dict | None = None):
        if not self.send_buffer:
            return None

        message_chains = self._split_message_chain_by_media(
            self.send_buffer,
            inline_images=self._should_inline_images(self.send_buffer),
        )
        stream_for_chain = stream if len(message_chains) == 1 else None

        ret = None
        for message_chain in message_chains:
            ret = await self._post_send_one(message_chain, stream_for_chain)

        self.send_buffer = None

        return ret

    async def _post_send_one(
        self,
        message_to_send: MessageChain,
        stream: dict | None = None,
    ):
        if not message_to_send:
            return None

        source = self.message_obj.raw_message

        if not isinstance(
            source,
            botpy.message.Message
            | botpy.message.GroupMessage
            | botpy.message.DirectMessage
            | botpy.message.C2CMessage
            | botpy.interaction.Interaction,
        ):
            logger.warning(f"[QQOfficial] 不支持的消息源类型: {type(source)}")
            return None

        # 先预扫消息链判断是否存在 keyboard / 裸按钮：有的话强制 markdown，
        # 并让 _parse_to_qqofficial 把图片转成 markdown 语法以便共存。
        # use_markdown_ 从 send_buffer（拆分前的原始链）读取，拆分出的 chunk 不携带该标记。
        use_md = getattr(self.send_buffer, "use_markdown_", None)
        has_keyboard_component = any(
            isinstance(seg, (QQCKeyboard, QQCButton)) for seg in message_to_send.chain
        )
        if has_keyboard_component and use_md is False:
            logger.warning("[QQOfficial] 检测到 QQC 按钮组件，自动启用 markdown 模式")
            use_md = True
        convert_img = has_keyboard_component and use_md is not False

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
            message_to_send,
            convert_image_to_markdown=convert_img,
        )
        if record_file_path:
            self.track_temporary_local_file(record_file_path)

        # C2C 流式仅用于文本分片，富媒体时降级为普通发送，避免平台侧流式校验报错。
        if stream and (
            image_base64 or record_file_path or video_file_source or file_source
        ):
            logger.debug("[QQOfficial] 检测到富媒体，降级为非流式发送。")
            stream = None

        if (
            not plain_text
            and not image_base64
            and not image_path
            and not record_file_path
            and not video_file_source
            and not file_source
            and not keyboard_payload
        ):
            return None

        # QQ C2C 流式 API 说明：
        # - 开始/中间分片（state=1）：增量追加内容，不需要 \n（加了会导致强制换行）
        # - 最终分片（state=10）：结束流，content 必须以 \n 结尾（QQ API 要求）
        if (
            stream
            and stream.get("state") == 10
            and plain_text
            and not plain_text.endswith("\n")
        ):
            plain_text = plain_text + "\n"

        # keyboard 要求 markdown content 非空，补零宽占位
        if keyboard_payload and not plain_text:
            plain_text = self.EMPTY_MARKDOWN_PLACEHOLDER

        is_interaction = isinstance(source, botpy.interaction.Interaction)
        if use_md is False:
            payload: dict = {
                "content": plain_text,
                "msg_type": 0,
            }
        else:
            payload = {
                "markdown": MarkdownPayload(content=plain_text) if plain_text else None,
                "msg_type": 2,
            }
            if keyboard_payload is not None:
                payload["keyboard"] = keyboard_payload

        # 按钮回调用 event_id 换取被动回复配额；其余用 msg_id。
        # message_id 在 _parse_interaction_to_abm 里已经被设为 interaction.event_id，
        # 这里两条分支只是字段名不同。
        if is_interaction:
            payload["event_id"] = self.message_obj.message_id
        else:
            payload["msg_id"] = self.message_obj.message_id

        if not isinstance(
            source,
            botpy.message.Message | botpy.message.DirectMessage,
        ):
            payload["msg_seq"] = random.randint(1, 10000)

        # 引用回复：QQ 仅允许纯文本(msg_type=0) 与图片富媒体(msg_type=7) 携带
        # message_reference；markdown(msg_type=2) 不允许，语音/视频/文件/keyboard 也不允许。
        if reference_message_id:
            # 群/C2C 带图片会被改写为 msg_type=7（markdown 被 pop），此时可引用；
            # 其余 use_md 非 False 的情况最终发送 markdown，不能引用。
            image_becomes_media = bool(image_base64) and isinstance(
                source, (botpy.message.GroupMessage, botpy.message.C2CMessage)
            )
            will_be_markdown = use_md is not False and not image_becomes_media
            has_ref_blocking = bool(
                record_file_path
                or video_file_source
                or file_source
                or keyboard_payload
                or will_be_markdown
            )
            if has_ref_blocking:
                logger.debug(
                    "[QQOfficial] 消息为 markdown 或含语音/视频/文件/按钮，忽略引用回复。"
                )
            else:
                ref_id = self._resolve_reference_id(source, reference_message_id)
                if ref_id:
                    payload["message_reference"] = Reference(
                        message_id=ref_id,
                        ignore_get_message_error=True,
                    )
                else:
                    logger.debug("[QQOfficial] 无法解析引用 REFIDX，跳过引用回复。")

        ret = None
        # 若 keyboard 和非 markdown-内联媒体同时存在，媒体路径会把 msg_type 改成 7
        # 并 pop markdown/keyboard。这里预先探测，稍后补发一条带 keyboard 的 markdown 消息。
        media_overrides_keyboard = keyboard_payload is not None and (
            image_base64 or record_file_path or video_file_source or file_source
        )
        if media_overrides_keyboard:
            payload.pop("keyboard", None)

        match source:
            case botpy.message.GroupMessage():
                if not source.group_openid:
                    logger.error("[QQOfficial] GroupMessage 缺少 group_openid")
                    return None

                if image_base64:
                    media = await self.upload_group_and_c2c_image(
                        image_base64,
                        self.IMAGE_FILE_TYPE,
                        group_openid=source.group_openid,
                    )
                    payload["media"] = media
                    payload["msg_type"] = 7
                    payload.pop("markdown", None)
                    payload["content"] = plain_text or None
                if record_file_path:  # group record msg
                    media = await self.upload_group_and_c2c_media(
                        record_file_path,
                        self.VOICE_FILE_TYPE,
                        group_openid=source.group_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                if video_file_source:
                    media = await self.upload_group_and_c2c_media(
                        video_file_source,
                        self.VIDEO_FILE_TYPE,
                        group_openid=source.group_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                if file_source:
                    media = await self.upload_group_and_c2c_media(
                        file_source,
                        self.FILE_FILE_TYPE,
                        file_name=file_name,
                        group_openid=source.group_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                ret = await self._send_with_stream_newline_fix(
                    send_func=lambda retry_payload: self.bot.api.post_group_message(
                        group_openid=source.group_openid,  # type: ignore
                        **retry_payload,
                    ),
                    payload=payload,
                    stream=stream,
                )

            case botpy.message.C2CMessage():
                if image_base64:
                    media = await self.upload_group_and_c2c_image(
                        image_base64,
                        self.IMAGE_FILE_TYPE,
                        openid=source.author.user_openid,
                    )
                    payload["media"] = media
                    payload["msg_type"] = 7
                    payload.pop("markdown", None)
                    payload["content"] = plain_text or None
                if record_file_path:  # c2c record
                    media = await self.upload_group_and_c2c_media(
                        record_file_path,
                        self.VOICE_FILE_TYPE,
                        openid=source.author.user_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                if video_file_source:
                    media = await self.upload_group_and_c2c_media(
                        video_file_source,
                        self.VIDEO_FILE_TYPE,
                        openid=source.author.user_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                if file_source:
                    media = await self.upload_group_and_c2c_media(
                        file_source,
                        self.FILE_FILE_TYPE,
                        file_name=file_name,
                        openid=source.author.user_openid,
                    )
                    if media:
                        payload["media"] = media
                        payload["msg_type"] = 7
                        payload.pop("markdown", None)
                        payload["content"] = plain_text or None
                if stream:
                    ret = await self._send_with_stream_newline_fix(
                        send_func=lambda retry_payload: self.post_c2c_message(
                            openid=source.author.user_openid,
                            **retry_payload,
                            stream=stream,
                        ),
                        payload=payload,
                        stream=stream,
                    )
                else:
                    ret = await self._send_with_stream_newline_fix(
                        send_func=lambda retry_payload: self.post_c2c_message(
                            openid=source.author.user_openid,
                            **retry_payload,
                        ),
                        payload=payload,
                        stream=stream,
                    )
                logger.debug(f"Message sent to C2C: {ret}")

            case botpy.message.Message():
                if image_path:
                    payload["file_image"] = image_path
                # Guild text-channel send API (/channels/{channel_id}/messages) does not use v2 msg_type.
                payload.pop("msg_type", None)
                ret = await self._send_with_stream_newline_fix(
                    send_func=lambda retry_payload: self.bot.api.post_message(
                        channel_id=source.channel_id,
                        **retry_payload,
                    ),
                    payload=payload,
                    stream=stream,
                )

            case botpy.message.DirectMessage():
                if image_path:
                    payload["file_image"] = image_path
                # Guild DM send API (/dms/{guild_id}/messages) does not use v2 msg_type.
                payload.pop("msg_type", None)
                ret = await self._send_with_stream_newline_fix(
                    send_func=lambda retry_payload: self.bot.api.post_dms(
                        guild_id=source.guild_id,
                        **retry_payload,
                    ),
                    payload=payload,
                    stream=stream,
                )

            case botpy.interaction.Interaction():
                # 按钮点击回调的回复：按 chat_type 路由
                # chat_type: 0=频道 / 1=群 / 2=C2C
                #
                # 已知限制：本分支不上传 QQ 富媒体（msg_type=7），因此不支持语音/视频/文件
                if record_file_path or video_file_source or file_source:
                    logger.warning(
                        "[QQOfficial] Interaction 回调暂不支持发送语音/视频/文件，"
                        "本次发送已跳过（chain 中检测到非图片媒体）。"
                    )
                    return None
                chat_type = source.chat_type
                if chat_type == 1 and source.group_openid:
                    ret = await self._send_with_stream_newline_fix(
                        send_func=lambda retry_payload: self.bot.api.post_group_message(
                            group_openid=source.group_openid,  # type: ignore
                            **retry_payload,
                        ),
                        payload=payload,
                        stream=stream,
                    )
                elif chat_type == 2 and source.user_openid:
                    ret = await self._send_with_stream_newline_fix(
                        send_func=lambda retry_payload: self.post_c2c_message(
                            openid=source.user_openid,  # type: ignore
                            **retry_payload,
                        ),
                        payload=payload,
                        stream=stream,
                    )
                elif chat_type == 0 and source.channel_id:
                    # 频道：v1 接口不接受 msg_type / msg_seq / event_id
                    guild_payload = payload.copy()
                    guild_payload.pop("msg_type", None)
                    guild_payload.pop("msg_seq", None)
                    # 频道接口用 msg_id 或 event_id 都可，保留 event_id
                    ret = await self._send_with_stream_newline_fix(
                        send_func=lambda retry_payload: self.bot.api.post_message(
                            channel_id=source.channel_id,  # type: ignore
                            **retry_payload,
                        ),
                        payload=guild_payload,
                        stream=stream,
                    )
                else:
                    logger.warning(
                        "[QQOfficial] interaction 无法路由: chat_type=%s",
                        chat_type,
                    )

            case _:
                pass

        # 非图片媒体抢占了 msg_type=7，补发一条 markdown+keyboard
        if media_overrides_keyboard and keyboard_payload:
            await self._send_keyboard_followup(source, plain_text, keyboard_payload)

        await super().send(message_to_send)

        return ret

    async def _send_keyboard_followup(
        self,
        source,
        plain_text: str,
        keyboard_payload: dict,
    ) -> None:
        """在媒体消息之后补发一条仅含 markdown+keyboard 的 msg_type=2 消息。"""
        content = plain_text or self.EMPTY_MARKDOWN_PLACEHOLDER
        followup: dict = {
            "markdown": MarkdownPayload(content=content),
            "msg_type": 2,
            "msg_id": self.message_obj.message_id,
            "keyboard": keyboard_payload,
            "msg_seq": random.randint(1, 10000),
        }
        try:
            if isinstance(source, botpy.message.GroupMessage):
                if not source.group_openid:
                    return
                await self.bot.api.post_group_message(
                    group_openid=source.group_openid,
                    **followup,
                )
            elif isinstance(source, botpy.message.C2CMessage):
                await self.post_c2c_message(
                    openid=source.author.user_openid,
                    **followup,
                )
            else:
                logger.debug(
                    "[QQOfficial] 消息源 %s 不支持 keyboard，忽略补发", type(source)
                )
        except Exception as e:
            logger.warning(f"[QQOfficial] keyboard 补发失败: {e}")

    def is_button_interaction(self) -> bool:
        """当前事件是否来自 QQ 消息按钮点击回调。"""
        raw = getattr(self.message_obj, "raw_message", None)
        return isinstance(raw, botpy.interaction.Interaction)

    def get_message_outline(self) -> str:
        """interaction 事件没有消息链，构造按钮摘要供日志使用。"""
        if not self.is_button_interaction():
            return super().get_message_outline()
        button_id = self.get_interaction_button_id() or "?"
        button_data = self.get_interaction_button_data()
        if button_data:
            return f"[Button] id={button_id} data={button_data}"
        return f"[Button] id={button_id}"

    def get_interaction_button_id(self) -> str:
        """获取被点击按钮的 id（`QQCButton.id`）；非交互事件返回空串。"""
        if not self.is_button_interaction():
            return ""
        raw = cast(botpy.interaction.Interaction, self.message_obj.raw_message)
        resolved = getattr(getattr(raw, "data", None), "resolved", None)
        return getattr(resolved, "button_id", "") or ""

    def get_interaction_button_data(self) -> str:
        """获取被点击按钮的 data（`QQCButton.data`）；非交互事件返回空串。"""
        if not self.is_button_interaction():
            return ""
        raw = cast(botpy.interaction.Interaction, self.message_obj.raw_message)
        resolved = getattr(getattr(raw, "data", None), "resolved", None)
        return getattr(resolved, "button_data", "") or ""

    async def _send_with_stream_newline_fix(
        self,
        send_func,
        payload: dict,
        stream: dict | None = None,
    ):
        """发送包装：流式 markdown 分片若因缺失换行被拒，补 `\\n` 重试一次。"""
        try:
            return await send_func(payload)
        except _QQOFFICIAL_SEND_API_ERRORS as err:
            logger.info("[QQOfficial] 回复消息失败: %s, 尝试使用主动发送接口。", err)
            if payload.get("msg_id"):
                fallback_payload = payload.copy()
                fallback_payload.pop("msg_id", None)
                try:
                    ret = await send_func(fallback_payload)
                    logger.info("[QQOfficial] 使用主动发送接口发送成功。")
                    return ret
                except _QQOFFICIAL_SEND_API_ERRORS as fallback_err:
                    err = fallback_err
                    payload = fallback_payload

            if not isinstance(err, botpy.errors.ServerError):
                raise

            # QQ 流式 markdown 分片校验：内容必须以换行结尾。
            # 某些边界场景服务端仍可能判定失败，这里做一次修正重试。
            if stream and self.STREAM_MARKDOWN_NEWLINE_ERROR in str(err):
                retry_payload = payload.copy()

                markdown_payload = retry_payload.get("markdown")
                if isinstance(markdown_payload, dict):
                    md_content = cast(str, markdown_payload.get("content", "") or "")
                    if md_content and not md_content.endswith("\n"):
                        retry_payload["markdown"] = {"content": md_content + "\n"}

                content = cast(str | None, retry_payload.get("content"))
                if content and not content.endswith("\n"):
                    retry_payload["content"] = content + "\n"

                logger.warning(
                    "[QQOfficial] 流式 markdown 分片换行校验失败，已修正后重试一次。"
                )
                return await send_func(retry_payload)
            raise

    async def upload_group_and_c2c_image(
        self,
        image_base64: str,
        file_type: int,
        **kwargs,
    ) -> botpy.types.message.Media:
        payload = {
            "file_data": image_base64,
            "file_type": file_type,
            "srv_send_msg": False,
        }

        @_qqofficial_retry()
        async def _do_upload():
            if "openid" in kwargs:
                payload["openid"] = kwargs["openid"]
                route = Route(
                    "POST", "/v2/users/{openid}/files", openid=kwargs["openid"]
                )
            elif "group_openid" in kwargs:
                payload["group_openid"] = kwargs["group_openid"]
                route = Route(
                    "POST",
                    "/v2/groups/{group_openid}/files",
                    group_openid=kwargs["group_openid"],
                )
            else:
                raise ValueError("Invalid upload parameters")

            result = await self.bot.api._http.request(route, json=payload)
            if result is None:
                err_msg = "上传图片API返回None，触发重试"
                raise APIReturnNoneError(err_msg)
            return result

        try:
            result = await _do_upload()
        except APIReturnNoneError:
            logger.warning(f"上传图片API返回None，共尝试5次后放弃: {payload}")
            raise

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Failed to upload image, response is not dict: {result}"
            )

        return Media(
            file_uuid=result["file_uuid"],
            file_info=result["file_info"],
            ttl=result.get("ttl", 0),
        )

    async def upload_group_and_c2c_media(
        self,
        file_source: str,
        file_type: int,
        srv_send_msg: bool = False,
        file_name: str | None = None,
        **kwargs,
    ) -> Media | None:
        """上传媒体文件"""
        # 构建基础payload
        payload: dict = {"file_type": file_type, "srv_send_msg": srv_send_msg}
        if file_name:
            payload["file_name"] = file_name

        # 处理文件数据
        if os.path.exists(file_source):
            # 读取本地文件
            async with aiofiles.open(file_source, "rb") as f:
                file_content = await f.read()
                # use base64 encode
                payload["file_data"] = base64.b64encode(file_content).decode("utf-8")
        else:
            # 使用URL
            payload["url"] = file_source

        # 添加接收者信息和确定路由
        if "openid" in kwargs:
            payload["openid"] = kwargs["openid"]
            route = Route("POST", "/v2/users/{openid}/files", openid=kwargs["openid"])
        elif "group_openid" in kwargs:
            payload["group_openid"] = kwargs["group_openid"]
            route = Route(
                "POST",
                "/v2/groups/{group_openid}/files",
                group_openid=kwargs["group_openid"],
            )
        else:
            return None

        @_qqofficial_retry()
        async def _do_upload():
            result = await self.bot.api._http.request(route, json=payload)
            if result is None:
                err_msg = "上传文件API返回None，触发重试"
                raise APIReturnNoneError(err_msg)
            return result

        try:
            result = await _do_upload()

            if result:
                if not isinstance(result, dict):
                    logger.error(f"上传文件响应格式错误: {result}")
                    return None

                return Media(
                    file_uuid=result["file_uuid"],
                    file_info=result["file_info"],
                    ttl=result.get("ttl", 0),
                )
        except APIReturnNoneError:
            logger.warning(f"上传文件API返回None，共尝试5次后放弃: {file_source}")
        except (botpy.errors.ServerError, botpy.errors.SequenceNumberError):
            logger.error(f"上传媒体文件失败，共尝试5次后放弃: {file_source}")
        except Exception as e:
            logger.error(f"上传请求错误: {e}")

        return None

    async def post_c2c_message(
        self,
        openid: str,
        msg_type: int = 0,
        content: str | None = None,
        embed: message.Embed | None = None,
        ark: message.Ark | None = None,
        message_reference: message.Reference | None = None,
        media: message.Media | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = 1,
        event_id: str | None = None,
        markdown: message.MarkdownPayload | None = None,
        keyboard: message.Keyboard | None = None,
        stream: dict | None = None,
    ) -> message.Message | None:
        payload = locals()
        payload.pop("self", None)
        if payload.get("msg_id") is None:
            payload.pop("msg_id", None)
        # QQ API does not accept stream.id=None; remove it when not yet assigned
        if "stream" in payload and payload["stream"] is not None:
            stream_data = dict(payload["stream"])
            if stream_data.get("id") is None:
                stream_data.pop("id", None)
            payload["stream"] = stream_data
        route = Route("POST", "/v2/users/{openid}/messages", openid=openid)

        retry_times = 3

        @_qqofficial_retry(retry_times)
        async def _do_request():
            result = await self.bot.api._http.request(route, json=payload)
            if result is None:
                err_msg = "发送消息API返回None，触发重试"
                raise APIReturnNoneError(err_msg)
            return result

        result = None
        try:
            result = await _do_request()
        except APIReturnNoneError:
            logger.warning(
                f"[QQOfficial] post_c2c_message: 发送消息失败，API 返回 None，共尝试{retry_times}次后放弃"
            )
            return None

        if not isinstance(result, dict):
            logger.error(f"[QQOfficial] post_c2c_message: 响应不是 dict: {result}")
            return None

        return message.Message(**result)

    @staticmethod
    async def _parse_to_qqofficial(
        message: MessageChain,
        convert_image_to_markdown: bool = False,
    ):
        """将 MessageChain 解析为发送 payload 所需要素。

        Args:
            message: 消息链
            convert_image_to_markdown: 若为 True 且图片能注册到文件服务，则将图片
                转成 markdown `![](url)` 语法追加到 plain_text，并跳过 base64 上传；
                这样图片能和 keyboard/markdown 共存于同一条 msg_type=2 消息。

        Returns:
            (plain_text, image_base64, image_file_path, record_file_path,
             video_file_source, file_source, file_name, keyboard_payload,
             reference_message_id)
        """
        plain_text = ""
        image_base64 = None  # only one img supported for msg_type=7 path
        image_file_path = None
        record_file_path = None
        video_file_source = None
        file_source = None
        file_name = None
        keyboard_payload: dict | None = None
        reference_message_id: str | None = None
        pending_buttons: list[QQCButton] = []
        for i in message.chain:
            if isinstance(i, Plain):
                plain_text += i.text
            elif isinstance(i, Reply):
                # 引用回复：取被引用消息 ID，实际填充在 _post_send_one
                reference_message_id = str(i.id) if i.id is not None else None
            elif isinstance(i, QQCKeyboard):
                keyboard_payload = i.to_dict()
            elif isinstance(i, QQCButton):
                pending_buttons.append(i)
            elif isinstance(i, Image):
                # markdown 模式下尽量把图片转成 markdown 语法，以便与 keyboard 共存
                if convert_image_to_markdown:
                    fragment = await image_to_markdown_fragment(i)
                    if fragment is not None:
                        plain_text += fragment
                        continue
                    # 失败时回退到 msg_type=7 路径
                    logger.warning(
                        "[QQOfficial] 图片转 markdown 失败，回退到 msg_type=7；"
                        "若消息链包含 keyboard 则 keyboard 会被丢弃。"
                    )
                if image_base64:
                    continue  # msg_type=7 路径只带第一张
                if not i.file:
                    raise ValueError("Unsupported image file format")
                image_is_local = is_file_uri(i.file)
                if not image_is_local:
                    try:
                        image_is_local = os.path.exists(i.file)
                    except OSError:
                        image_is_local = False
                resolver = MediaResolver(i.file, media_type="image")
                if image_is_local:
                    async with resolver.as_path() as resolved:
                        image_file_path = str(resolved.path.resolve())
                        image_base64 = resolved.to_base64()
                else:
                    image_base64 = await resolver.to_base64()
            elif isinstance(i, Record):
                record_ref = i.url or i.file
                if record_ref:
                    try:
                        record_file_path = await MediaResolver(
                            record_ref,
                            media_type="audio",
                            default_suffix=".wav",
                        ).to_path(
                            target_format="tencent_silk",
                        )
                    except Exception as e:
                        logger.error(f"处理语音时出错: {e}")
                        record_file_path = None
            elif isinstance(i, Video) and not video_file_source:
                if is_file_uri(i.file):
                    video_file_source = file_uri_to_path(i.file)
                else:
                    video_file_source = i.file
            elif isinstance(i, File) and not file_source:
                file_name = i.name
                if i.file_:
                    file_path = i.file_
                    if is_file_uri(file_path):
                        file_path = file_uri_to_path(file_path)
                    file_source = file_path
                elif i.url:
                    file_source = i.url
            else:
                logger.debug(f"qq_official 忽略 {i.type}")

        # 裸 QQCButton 自动包一层 keyboard（仅当未显式传 QQCKeyboard 时）
        if keyboard_payload is None and pending_buttons:
            keyboard_payload = QQCKeyboard(rows=[pending_buttons]).to_dict()

        return (
            plain_text,
            image_base64,
            image_file_path,
            record_file_path,
            video_file_source,
            file_source,
            file_name,
            keyboard_payload,
            reference_message_id,
        )
