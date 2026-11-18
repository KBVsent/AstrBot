"""Records platform messages for the WebUI SNS client and expires them."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.core.db.po import ImMessage, ImMessageWrite, ImSession
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    Image,
    Plain,
    Reply,
)
from astrbot.core.umo_alias import get_event_auto_name, normalize_umo_name, parse_umo
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

if TYPE_CHECKING:
    from astrbot.core.db import BaseDatabase
    from astrbot.core.platform.astr_message_event import AstrMessageEvent

SNS_UMO_EXTRA = "_sns_umo"
"""Event extra holding the UMO from before session isolation."""

_RECORDED_EXTRA = "_sns_recorded"
_QUOTE_EXTRA = "_sns_quote"
"""Event extra holding (sender name, preview) for replies that quote the event."""

sns_send_suppressed: ContextVar[bool] = ContextVar("sns_send_suppressed", default=False)
"""Set while an outer send records the message itself, so nested event sends skip it."""

_active_writer: SnsMessageWriter | None = None


def get_sns_writer() -> SnsMessageWriter | None:
    """Return the running writer, or None before startup and after shutdown."""
    return _active_writer


def _clip(text: str) -> str:
    return " ".join(text.split())[:80]


def _image_source(
    image: Image, temp_root: str, sent_images: list[tuple[str, str]] | None
) -> dict[str, str]:
    """Classify where an image can be fetched later, using string checks only."""
    refs = [ref for ref in (image.url, image.file, image.path) if ref]
    # What the bot sends may sit anywhere in the data directory, the parent of temp.
    local_root = temp_root if sent_images is None else os.path.dirname(temp_root)
    for ref in refs:
        if ref.startswith(("http://", "https://")):
            return {"kind": "url", "value": ref}
    for ref in refs:
        path = file_uri_to_path(ref) if is_file_uri(ref) else ref
        if not os.path.isabs(path):
            continue
        path = os.path.normpath(path)
        if path.startswith(local_root + os.sep):
            return {"kind": "file", "value": path}
    for ref in refs:
        if sent_images is not None and ref.startswith("base64://"):
            path = os.path.join(temp_root, "sns_images", uuid.uuid4().hex)
            sent_images.append((path, ref.removeprefix("base64://")))
            return {"kind": "file", "value": path}
    return {"kind": "none", "value": ""}


def serialize_chain(
    chain: Iterable[BaseMessageComponent],
    platform_name: str,
    temp_root: str,
    sent_images: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Convert a message chain to stored content segments without any IO.

    Args:
        chain: Components as delivered by the adapter or passed to send.
        platform_name: Adapter type, used for adapter-specific markers.
        temp_root: Resolved temp directory; only images inside it count as files.
        sent_images: Given for messages the bot sends. In-memory images get a
            path under temp and are appended as (path, base64) for the caller
            to save.
    """
    placeholders = {
        "Record": "[语音]",
        "Video": "[视频]",
        "File": "[文件]",
        "Face": "[表情]",
        "Forward": "[转发消息]",
        "Node": "[转发消息]",
        "Nodes": "[转发消息]",
        "Poke": "[戳一戳]",
        "Share": "[分享]",
        "Music": "[音乐]",
        "Json": "[卡片]",
        "Location": "[位置]",
        "Contact": "[名片]",
        "RPS": "[猜拳]",
        "Dice": "[骰子]",
        "Shake": "[窗口抖动]",
        "ActionRow": "[按钮]",
        "Button": "[按钮]",
        "ButtonInteraction": "[按钮]",
        "discord_view": "[按钮]",
        "qqc_keyboard": "[按钮]",
        "qqc_button": "[按钮]",
        "discord_embed": "[卡片]",
        "line_flex": "[卡片]",
    }
    segments: list[dict[str, Any]] = []
    for component in chain:
        if getattr(component, "is_control_component", False):
            continue
        if isinstance(component, Plain):
            if component.text:
                segments.append({"type": "Plain", "text": component.text})
        elif isinstance(component, At):
            target = str(component.qq)
            # The QQ official adapter inserts this marker into C2C and channel messages.
            if target == "qq_official" and platform_name in (
                "qq_official",
                "qq_official_webhook",
            ):
                continue
            segments.append(
                {"type": "At", "target": target, "name": component.name or ""}
            )
        elif isinstance(component, Reply):
            # Adapters word their own placeholders, so the chain comes first.
            quoted = (
                preview_text(
                    serialize_chain(component.chain or [], platform_name, temp_root)
                )
                or component.message_str
                or ""
            )
            segments.append(
                {
                    "type": "Reply",
                    "id": str(component.id),
                    "sender_name": component.sender_nickname or "",
                    "text": _clip(quoted),
                }
            )
        elif isinstance(component, Image):
            segments.append(
                {
                    "type": "Image",
                    "src": _image_source(component, temp_root, sent_images),
                }
            )
        else:
            name = str(getattr(component.type, "value", component.type))
            segments.append(
                {
                    "type": "Other",
                    "name": name,
                    "text": placeholders.get(name, "[消息]"),
                }
            )
    return segments


def preview_text(segments: list[dict[str, Any]]) -> str:
    """Build the one-line session preview; quoted content is left out.

    Non-text segments become [ComponentType] markers that the WebUI localizes.
    """
    parts: list[str] = []
    for segment in segments:
        kind = segment["type"]
        if kind == "Plain":
            parts.append(segment["text"])
        elif kind == "Other":
            parts.append(f"[{segment['name']}]")
        elif kind == "Image":
            parts.append("[Image]")
        elif kind == "Click":
            parts.append(f"[ButtonInteraction] {segment['text']}")
        elif kind == "At":
            parts.append("@" + (segment["name"] or segment["target"]))
    return _clip(" ".join(parts))


class SnsMessageWriter:
    """Queues message records and commits them in batches from one task.

    Record methods never touch the database, so callers on the dispatch and
    send paths do not wait for it.
    """

    def __init__(self, db: BaseDatabase, config: dict) -> None:
        """Create an idle writer; install() and run() start it.

        Args:
            db: Main database.
            config: Default profile; sns_client.enable is read on every record.
        """
        self._db = db
        self._config = config
        self._temp_root = get_astrbot_temp_path()
        self._queue: asyncio.Queue[ImMessageWrite] = asyncio.Queue(maxsize=10_000)
        self._writing: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[ImMessage, ImSession], None]] = []
        self._dropped = 0
        self._unsaved_images: list[tuple[str, str]] = []

    @property
    def enabled(self) -> bool:
        settings = self._config.get("sns_client") or {}
        return bool(settings.get("enable", True))

    def install(self) -> None:
        """Make this writer the one returned by get_sns_writer."""
        global _active_writer
        _active_writer = self

    def add_listener(self, listener: Callable[[ImMessage, ImSession], None]) -> None:
        """Call listener after each commit with every message and its session state."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[ImMessage, ImSession], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def record_inbound(self, event: AstrMessageEvent) -> None:
        """Snapshot an inbound event before the pipeline rewrites its chain.

        Must run before session isolation, since the stored UMO is the original one.
        """
        try:
            # A shallow-copied re-entry of the same message shares this extras dict.
            if not self.enabled or event.get_extra(_RECORDED_EXTRA):
                return
            event.set_extra(_RECORDED_EXTRA, True)
            umo = event.unified_msg_origin
            event.set_extra(SNS_UMO_EXTRA, umo)
            sender_id = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if (
                event.get_platform_name() == "webchat"
                or event.event_kind != "message"
                or (sender_id and sender_id == self_id)
            ):
                return
            if event.is_callback_event():
                label = event.describe_callback()
                if label is None:
                    return
                content = [{"type": "Click", "text": label[:300]}]
            else:
                content = serialize_chain(
                    event.get_messages(), event.get_platform_name(), self._temp_root
                )
            if not content:
                return
            for segment in content:
                # Adapters that mention by id alone would show the bot as a number.
                if (
                    segment["type"] == "At"
                    and not segment["name"]
                    and segment["target"] == self_id
                ):
                    segment["name"] = parse_umo(umo)["platform"]
            sender_name = normalize_umo_name(event.get_sender_name())
            event.set_extra(_QUOTE_EXTRA, (sender_name, preview_text(content)))
            platform_message_id = getattr(event.message_obj, "message_id", None)
            message = ImMessage(
                umo=umo,
                direction="in",
                platform_message_id=(
                    str(platform_message_id) if platform_message_id else None
                ),
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
            )
            self._enqueue(
                message, content, get_event_auto_name(event, fallback_to_id=False)
            )
        except Exception:
            logger.exception("Failed to record an inbound message for the SNS client.")

    def record_outbound(
        self,
        umo: str,
        platform_name: str,
        self_id: str,
        chain: list[BaseMessageComponent],
        event: AstrMessageEvent | None = None,
    ) -> None:
        """Record one send handed to an adapter; status is always ok.

        Args:
            umo: Session the message goes to, from before session isolation.
            platform_name: Adapter type.
            self_id: The bot's id on the platform, or empty when unknown.
            chain: Components handed to the adapter.
            event: Event being answered, the source of what a quote of it shows.
        """
        try:
            if (
                not chain
                or platform_name == "webchat"
                or sns_send_suppressed.get()
                or not self.enabled
            ):
                return
            content = serialize_chain(
                chain, platform_name, self._temp_root, self._unsaved_images
            )
            if event is not None and umo == (
                event.get_extra(SNS_UMO_EXTRA) or event.unified_msg_origin
            ):
                content = self._quote_trigger(event, content)
            if not content:
                return
            message = ImMessage(
                umo=umo, direction="out", sender_id=self_id or "", content=content
            )
            self._enqueue(message, content)
        except Exception:
            logger.exception("Failed to record an outbound message for the SNS client.")

    @staticmethod
    def _quote_trigger(
        event: AstrMessageEvent, content: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Complete quotes of the answered event, which arrive as a bare id."""
        trigger_id = str(getattr(event.message_obj, "message_id", "") or "")
        sender_name, text = event.get_extra(_QUOTE_EXTRA) or ("", "")
        kept = []
        for segment in content:
            if segment["type"] == "Reply" and segment["id"] == trigger_id:
                # A button press is not a message, so there is nothing to quote.
                if event.is_callback_event():
                    continue
                segment["sender_name"] = segment["sender_name"] or sender_name
                segment["text"] = segment["text"] or text
            kept.append(segment)
        return kept

    def _enqueue(
        self,
        message: ImMessage,
        content: list[dict[str, Any]],
        session_name: str = "",
    ) -> None:
        parsed = parse_umo(message.umo)
        write = ImMessageWrite(
            message=message,
            platform_id=parsed["platform"],
            message_type=parsed["message_type"],
            preview=preview_text(content),
            session_name=session_name,
        )
        try:
            self._queue.put_nowait(write)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(
                    f"SNS message queue is full; {self._dropped} records dropped so far."
                )

    def _take_batch(self) -> list[ImMessageWrite]:
        batch: list[ImMessageWrite] = []
        while len(batch) < 50 and not self._queue.empty():
            batch.append(self._queue.get_nowait())
        return batch

    async def _insert(
        self, batch: list[ImMessageWrite]
    ) -> list[tuple[ImMessage, ImSession]]:
        # A rolled-back insert leaves its assigned ids on the reused objects.
        for write in batch:
            write.message.id = None
        return await self._db.insert_im_messages(batch)

    @staticmethod
    def _save_images(images: list[tuple[str, str]]) -> None:
        for path, data in images:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as file:
                    file.write(base64.b64decode(data))
            except (OSError, ValueError) as exc:
                logger.warning(f"Failed to save a sent image for the SNS client: {exc}")

    async def _write(self, batch: list[ImMessageWrite]) -> None:
        if self._unsaved_images:
            images, self._unsaved_images = self._unsaved_images, []
            await asyncio.to_thread(self._save_images, images)
        results: list[tuple[ImMessage, ImSession]] | None = None
        for delay in (0, 1, 3):
            await asyncio.sleep(delay)
            try:
                results = await self._insert(batch)
                break
            except Exception as exc:
                logger.warning(f"Writing {len(batch)} SNS messages failed: {exc}")
        if results is None:
            # One bad record must not take the rest of the batch with it.
            results = []
            for write in batch:
                try:
                    results.extend(await self._insert([write]))
                except Exception:
                    logger.exception("Failed to write an SNS message; dropped.")
        for message, session in results:
            for listener in list(self._listeners):
                try:
                    listener(message, session)
                except Exception:
                    logger.exception("SNS message listener failed.")

    async def run(self) -> None:
        """Commit queued records until cancelled."""
        try:
            await self._db.fail_interrupted_im_sends()
        except Exception:
            logger.exception("Failed to settle interrupted SNS sends.")
        while True:
            first = await self._queue.get()
            batch = [first, *self._take_batch()]
            # Shielded so stop() cancelling this task cannot abort a batch already
            # taken off the queue; drain() waits for it instead.
            self._writing = asyncio.create_task(self._write(batch))
            await asyncio.shield(self._writing)
            await asyncio.sleep(0)

    async def drain(self) -> None:
        """Commit everything queued so far, whether or not run() is alive."""
        while True:
            writing = self._writing
            if writing is not None and not writing.done():
                await asyncio.wait([writing])
                continue
            batch = self._take_batch()
            if not batch:
                return
            await self._write(batch)

    async def close(self) -> None:
        """Drain the queue and stop accepting records."""
        await self.drain()
        global _active_writer
        if _active_writer is self:
            _active_writer = None


class SnsRetentionCleaner:
    """Deletes SNS messages older than sns_client.retention_days once a day."""

    def __init__(self, db: BaseDatabase, config: dict) -> None:
        self._db = db
        self._config = config
        self._stop_event = asyncio.Event()

    def _retention_days(self) -> int:
        settings = self._config.get("sns_client") or {}
        try:
            return int(settings.get("retention_days", 30))
        except (TypeError, ValueError):
            return 30

    async def cleanup_once(self) -> int:
        """Delete expired messages in short transactions; safe to rerun after a crash.

        Returns:
            Number of deleted messages.
        """
        days = self._retention_days()
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        batch_size = 5000
        total = 0
        while not self._stop_event.is_set():
            deleted = await self._db.purge_im_messages_batch(cutoff, batch_size)
            total += deleted
            if deleted < batch_size:
                break
            # Lets other writers waiting on the SQLite write lock in between.
            await asyncio.sleep(0.1)
        return total

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                deleted = await self.cleanup_once()
                if deleted:
                    logger.info(f"Deleted {deleted} expired SNS messages.")
            except Exception:
                logger.exception("SNS message cleanup failed.")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=24 * 3600)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop_event.set()
