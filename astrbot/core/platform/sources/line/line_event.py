import asyncio
import os
import re
import shutil
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Plain,
    Record,
    Video,
)
from astrbot.core import astrbot_config, file_token_service
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import get_media_duration

from .line_api import LineAPIClient

LINE_MAX_MESSAGES_PER_REPLY = 5


class LineMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str,
        message_obj,
        platform_meta,
        session_id,
        line_api: LineAPIClient,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.line_api = line_api

        # LINE 的 reply token 单次有效、仅约 1 分钟内可用、单次最多 5 条消息
        raw = message_obj.raw_message
        self._reply_token = (
            str(raw.get("replyToken") or "") if isinstance(raw, dict) else ""
        )
        self._pending_messages: list[dict] = []
        self._reply_dropped = 0
        self._flush_task: asyncio.Task | None = None
        self._flushed = False

    @staticmethod
    async def _component_to_message_object(
        segment: BaseMessageComponent,
    ) -> dict | None:
        if isinstance(segment, Plain):
            text = segment.text.strip()
            if not text:
                return None
            return {"type": "text", "text": text[:5000]}

        if isinstance(segment, At):
            name = str(segment.name or segment.qq or "").strip()
            if not name:
                return None
            return {"type": "text", "text": f"@{name}"[:5000]}

        if isinstance(segment, Image):
            original_url, preview_url = await LineMessageEvent._resolve_image_urls(
                segment
            )
            if not original_url or not preview_url:
                return None
            return {
                "type": "image",
                "originalContentUrl": original_url,
                "previewImageUrl": preview_url,
            }

        if isinstance(segment, Record):
            audio_url = await LineMessageEvent._resolve_record_url(segment)
            if not audio_url:
                return None
            duration = await LineMessageEvent._resolve_record_duration(segment)
            return {
                "type": "audio",
                "originalContentUrl": audio_url,
                "duration": duration,
            }

        if isinstance(segment, Video):
            video_url = await LineMessageEvent._resolve_video_url(segment)
            if not video_url:
                return None
            preview_url = await LineMessageEvent._resolve_video_preview_url(segment)
            if not preview_url:
                return None
            return {
                "type": "video",
                "originalContentUrl": video_url,
                "previewImageUrl": preview_url,
            }

        if isinstance(segment, File):
            file_url = await LineMessageEvent._resolve_file_url(segment)
            if not file_url:
                return None
            file_name = str(segment.name or "").strip() or "file.bin"
            file_size = await LineMessageEvent._resolve_file_size(segment)
            if file_size <= 0:
                return None
            return {
                "type": "file",
                "fileName": file_name,
                "fileSize": file_size,
                "originalContentUrl": file_url,
            }

        return None

    @staticmethod
    async def _resolve_image_urls(segment: Image) -> tuple[str, str]:
        candidate = (segment.url or segment.file or "").strip()
        if candidate.startswith("https://"):
            return candidate, candidate
        try:
            file_path = await segment.convert_to_file_path()
            urls = await LineMessageEvent._register_local_media(
                file_path, token_count=2
            )
            return urls[0], urls[1]
        except Exception as e:
            logger.debug("[LINE] resolve image url failed: %s", e)
            return "", ""

    @staticmethod
    async def _resolve_record_url(segment: Record) -> str:
        candidate = (segment.url or segment.file or "").strip()
        if candidate.startswith("https://"):
            return candidate
        try:
            file_path = await segment.convert_to_file_path()
            return (await LineMessageEvent._register_local_media(file_path))[0]
        except Exception as e:
            logger.debug("[LINE] resolve record url failed: %s", e)
            return ""

    @staticmethod
    async def _resolve_record_duration(segment: Record) -> int:
        try:
            file_path = await segment.convert_to_file_path()
            duration_ms = await get_media_duration(file_path)
            if isinstance(duration_ms, int) and duration_ms > 0:
                return duration_ms
        except Exception as e:
            logger.debug("[LINE] resolve record duration failed: %s", e)
        return 1000

    @staticmethod
    async def _resolve_video_url(segment: Video) -> str:
        candidate = (segment.file or "").strip()
        if candidate.startswith("https://"):
            return candidate
        try:
            file_path = await segment.convert_to_file_path()
            return (await LineMessageEvent._register_local_media(file_path))[0]
        except Exception as e:
            logger.debug("[LINE] resolve video url failed: %s", e)
            return ""

    @staticmethod
    async def _resolve_video_preview_url(segment: Video) -> str:
        cover_candidate = (segment.cover or "").strip()
        if cover_candidate.startswith("https://"):
            return cover_candidate

        if cover_candidate:
            try:
                cover_seg = Image(file=cover_candidate)
                cover_path = await cover_seg.convert_to_file_path()
                return (await LineMessageEvent._register_local_media(cover_path))[0]
            except Exception as e:
                logger.debug("[LINE] resolve video cover failed: %s", e)

        try:
            video_path = await segment.convert_to_file_path()
            temp_dir = Path(get_astrbot_temp_path())
            temp_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = temp_dir / f"line_video_preview_{uuid.uuid4().hex}.jpg"

            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-ss",
                "00:00:01",
                "-i",
                video_path,
                "-frames:v",
                "1",
                str(thumb_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            if process.returncode != 0 or not thumb_path.exists():
                return ""

            cover_seg = Image.fromFileSystem(str(thumb_path))
            cover_path = await cover_seg.convert_to_file_path()
            return (await LineMessageEvent._register_local_media(cover_path))[0]
        except Exception as e:
            logger.debug("[LINE] generate video preview failed: %s", e)
            return ""

    @staticmethod
    async def _resolve_file_url(segment: File) -> str:
        if segment.url and segment.url.startswith("https://"):
            return segment.url
        try:
            file_path = await segment.get_file(allow_return_url=False)
            if not file_path:
                return ""
            return (await LineMessageEvent._register_local_media(file_path))[0]
        except Exception as e:
            logger.debug("[LINE] resolve file url failed: %s", e)
            return ""

    @staticmethod
    async def _register_local_media(
        file_path: str,
        token_count: int = 1,
    ) -> list[str]:
        """Copy LINE media into global temp storage and register download tokens.

        Args:
            file_path: Local media path to expose to LINE.
            token_count: Number of single-use URLs to register for the media copy.

        Returns:
            Public callback URLs backed by the LINE-owned temporary copy.

        Raises:
            ValueError: The public callback base URL is not configured.
            FileNotFoundError: The local media file does not exist.
        """
        callback_host = str(astrbot_config.get("callback_api_base", "")).rstrip("/")
        if not callback_host:
            raise ValueError("未配置 callback_api_base，文件服务不可用")

        source_path = Path(file_path)
        if not source_path.is_file():
            raise FileNotFoundError(f"LINE media file does not exist: {file_path}")

        outbound_dir = Path(get_astrbot_temp_path()) / "line_outbound"
        outbound_dir.mkdir(parents=True, exist_ok=True)
        outbound_path = outbound_dir / f"{uuid.uuid4().hex}{source_path.suffix}"
        await asyncio.to_thread(shutil.copyfile, source_path, outbound_path)

        urls = []
        for _ in range(token_count):
            token = await file_token_service.register_file(str(outbound_path))
            urls.append(f"{callback_host}/api/file/{token}")
        logger.debug("[LINE] registered %s outbound media URL(s).", len(urls))
        return urls

    @staticmethod
    async def _resolve_file_size(segment: File) -> int:
        try:
            file_path = await segment.get_file(allow_return_url=False)
            if file_path and os.path.exists(file_path):
                return int(os.path.getsize(file_path))
        except Exception as e:
            logger.debug("[LINE] resolve file size failed: %s", e)
        return 0

    @classmethod
    async def build_line_messages(cls, message_chain: MessageChain) -> list[dict]:
        messages: list[dict] = []
        for segment in message_chain.chain:
            obj = await cls._component_to_message_object(segment)
            if obj:
                messages.append(obj)

        if not messages:
            return []

        if len(messages) > 5:
            logger.warning(
                "[LINE] message count exceeds 5, extra segments will be dropped."
            )
            messages = messages[:5]
        return messages

    async def send(self, message: MessageChain) -> None:
        messages = await self.build_line_messages(message)
        if messages:
            self._enqueue(messages)
        await super().send(message)

    def _enqueue(self, messages: list[dict]) -> None:
        """将消息累积到缓冲区，并确保已安排 pipeline 结束后的一次性发送。"""
        if self._flushed:
            # pipeline 已结束、reply token 已消耗，无法再回复。
            self._reply_dropped += len(messages)
            logger.warning(
                "[LINE] reply already sent, %s late message(s) dropped.",
                len(messages),
            )
            return

        remaining = LINE_MAX_MESSAGES_PER_REPLY - len(self._pending_messages)
        if remaining <= 0:
            self._reply_dropped += len(messages)
            return

        self._pending_messages.extend(messages[:remaining])
        if len(messages) > remaining:
            self._reply_dropped += len(messages) - remaining

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

        messages = self._pending_messages[:LINE_MAX_MESSAGES_PER_REPLY]
        self._pending_messages = []
        if self._reply_dropped:
            logger.warning(
                "[LINE] reply limited to %s messages, %s extra segment(s) dropped.",
                LINE_MAX_MESSAGES_PER_REPLY,
                self._reply_dropped,
            )
        if not messages:
            return

        if not self._reply_token:
            logger.warning(
                "[LINE] no reply token available, %s message(s) not sent.",
                len(messages),
            )
            return

        sent = await self.line_api.reply_message(self._reply_token, messages)
        if not sent:
            logger.error(
                "[LINE] reply failed (token may be invalid or expired), "
                "%s message(s) not sent.",
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
