from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from astrbot.core import logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.core.db.po import ImMessage, ImMessageWrite, ImSession, UmoAlias
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.message_type import MessageType
from astrbot.core.sns_recorder import (
    SnsMessageWriter,
    preview_text,
    sns_send_suppressed,
)
from astrbot.core.umo_alias import (
    build_umo_alias_map,
    get_umo_display_name,
    parse_umo,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.datetime_utils import to_utc_isoformat
from astrbot.core.utils.media_utils import (
    detect_image_mime_type,
    detect_image_mime_type_async,
)


class SnsServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class SnsImage:
    """An image to send back: a local file or a proxied upstream body."""

    media_type: str
    path: Path | None = None
    chunks: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    """Releases the upstream connection even if chunks is never iterated."""


class SnsBroadcaster:
    """Fans events out to SSE subscribers, each with its own bounded queue."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[tuple[str, dict]]] = set()

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def subscribe(self) -> asyncio.Queue[tuple[str, dict]]:
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: str, data: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # The client refetches everything on resync, so the backlog is useless.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(("resync", {}))


class SnsService:
    def __init__(self, db: BaseDatabase, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.db = db
        self.core_lifecycle = core_lifecycle
        self.broadcaster = SnsBroadcaster()
        # Events wait here so message.created can be named without reordering events.
        self._outbox: deque[tuple[str, dict, ImSession | None]] = deque()
        self._flushing: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._deliveries: set[asyncio.Task[None]] = set()
        self._http: httpx.AsyncClient | None = None
        # The dashboard is built after core initialize(), so the writer already exists
        # even though install() has not run yet.
        if (writer := self._writer()) is not None:
            writer.add_listener(self._on_message_committed)

    def _writer(self) -> SnsMessageWriter | None:
        return getattr(self.core_lifecycle, "sns_writer", None)

    def _platform_types(self) -> dict[str, str]:
        types = {
            str(item["id"]): str(item.get("type") or "")
            for item in self.core_lifecycle.astrbot_config.get("platform", [])
            if isinstance(item, dict) and item.get("id")
        }
        for platform in self.core_lifecycle.platform_manager.platform_insts:
            meta = platform.meta()
            types[meta.id] = meta.name
        return types

    @staticmethod
    def _session_dict(
        session: ImSession, alias: UmoAlias | None, platform_types: dict[str, str]
    ) -> dict[str, Any]:
        session_id = parse_umo(session.umo)["session_id"]
        return {
            "umo": session.umo,
            "platform_id": session.platform_id,
            "platform_type": platform_types.get(session.platform_id, ""),
            "message_type": session.message_type,
            "session_id": session_id,
            # The session id, not the whole UMO, is the last fallback.
            "display_name": get_umo_display_name(
                umo=session_id,
                auto_name=alias.auto_name if alias else None,
                user_alias=alias.user_alias if alias else None,
            ),
            "last_message_id": session.last_message_id,
            "last_preview": session.last_preview,
            "last_sender_name": session.last_sender_name,
            "last_time": to_utc_isoformat(session.last_time),
            "unread_count": session.unread_count,
            "pinned": bool(session.pinned),
            "keep_forever": bool(session.keep_forever),
            "purged_until": to_utc_isoformat(session.purged_until),
        }

    @staticmethod
    def _message_dict(message: ImMessage) -> dict[str, Any]:
        content = []
        for segment in message.content or []:
            if segment.get("type") == "Image":
                # src never leaves the server: Telegram file links embed the bot token.
                kind = (segment.get("src") or {}).get("kind")
                content.append({"type": "Image", "available": kind in ("url", "file")})
            else:
                content.append(segment)
        return {
            "id": message.id,
            "umo": message.umo,
            "direction": message.direction,
            "platform_message_id": message.platform_message_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "content": content,
            "source": message.source,
            "client_id": message.client_id,
            "status": message.status,
            "error": message.error,
            "created_at": to_utc_isoformat(message.created_at),
        }

    async def _session_payload(self, session: ImSession) -> dict[str, Any]:
        alias = await self.db.get_umo_alias(session.umo)
        return self._session_dict(session, alias, self._platform_types())

    # Events

    def _enqueue(
        self, event: str, data: dict, session: ImSession | None = None
    ) -> None:
        """Queue an event; a given session is named and added as data["session"]."""
        if not self.broadcaster.has_subscribers:
            return
        self._outbox.append((event, data, session))
        if self._flushing is None or self._flushing.done():
            self._flushing = asyncio.create_task(self._flush_outbox())

    async def _flush_outbox(self) -> None:
        while self._outbox:
            batch = list(self._outbox)
            self._outbox.clear()
            umos = list({session.umo for _, _, session in batch if session})
            aliases: dict[str, UmoAlias] = {}
            if umos:
                try:
                    aliases = build_umo_alias_map(await self.db.get_umo_aliases(umos))
                except Exception:
                    logger.exception("Failed to load SNS session names for events.")
            platform_types = self._platform_types()
            for event, data, session in batch:
                if session is not None:
                    data = {
                        **data,
                        "session": self._session_dict(
                            session, aliases.get(session.umo), platform_types
                        ),
                    }
                self.broadcaster.publish(event, data)

    def _on_message_committed(self, message: ImMessage, session: ImSession) -> None:
        self._enqueue(
            "message.created", {"message": self._message_dict(message)}, session
        )

    async def stream_events(self) -> AsyncGenerator[str, None]:
        queue = self.broadcaster.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                payload = json.dumps(data, ensure_ascii=False)
                yield f"event: {event}\ndata: {payload}\n\n"
        finally:
            self.broadcaster.unsubscribe(queue)

    # Sessions

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[bool, datetime, str]:
        try:
            pinned, last_time, umo = json.loads(base64.urlsafe_b64decode(cursor))
            return bool(pinned), datetime.fromisoformat(last_time), str(umo)
        except (ValueError, TypeError) as exc:
            raise SnsServiceError("Invalid cursor") from exc

    @staticmethod
    def _encode_cursor(session: ImSession) -> str:
        key = [bool(session.pinned), session.last_time.isoformat(), session.umo]
        return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()

    async def list_sessions(
        self,
        *,
        message_type: str | None,
        platform_id: str | None,
        query: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        rows = await self.db.list_im_sessions(
            message_type=message_type,
            platform_id=platform_id,
            query=query,
            after=self._decode_cursor(cursor) if cursor else None,
            limit=limit + 1,
        )
        page = rows[:limit]
        platform_types = self._platform_types()
        platforms = []
        for platform in self.core_lifecycle.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.name != "webchat":
                platforms.append({"id": meta.id, "type": meta.name})
        return {
            "sessions": [
                self._session_dict(session, alias, platform_types)
                for session, alias in page
            ],
            "next_cursor": (
                self._encode_cursor(page[-1][0]) if len(rows) > limit else None
            ),
            "unread_sessions": await self.db.count_unread_im_sessions(),
            "platforms": platforms,
        }

    async def _update_session(self, umo: str, **values: Any) -> dict[str, Any]:
        session = await self.db.update_im_session(umo, **values)
        if session is None:
            raise SnsServiceError("Session not found", status_code=404)
        payload = await self._session_payload(session)
        self._enqueue("session.updated", {"session": payload})
        return payload

    async def patch_session(
        self, umo: str, *, pinned: bool | None, keep_forever: bool | None
    ) -> dict[str, Any]:
        values = {
            key: value
            for key, value in (("pinned", pinned), ("keep_forever", keep_forever))
            if value is not None
        }
        if not values:
            raise SnsServiceError("Nothing to update")
        return await self._update_session(umo, **values)

    async def mark_read(self, umo: str) -> dict[str, Any]:
        return await self._update_session(umo, unread_count=0)

    async def delete_session(self, umo: str) -> None:
        # Queued records of this session would recreate it after the delete.
        if (writer := self._writer()) is not None:
            await writer.drain()
        if not await self.db.delete_im_session(umo):
            raise SnsServiceError("Session not found", status_code=404)
        self._enqueue("session.deleted", {"umo": umo})

    # Messages

    async def list_messages(
        self,
        umo: str,
        *,
        before_id: int | None,
        after_id: int | None,
        limit: int,
    ) -> dict[str, Any]:
        if before_id is not None and after_id is not None:
            raise SnsServiceError("before_id and after_id cannot be combined")
        session = await self.db.get_im_session(umo)
        if session is None:
            raise SnsServiceError("Session not found", status_code=404)
        messages = await self.db.list_im_messages(
            umo, before_id=before_id, after_id=after_id, limit=limit + 1
        )
        has_more = len(messages) > limit
        if has_more:
            # The extra row sits on the far side of the page from the cursor.
            messages = messages[:limit] if after_id is not None else messages[1:]
        return {
            "messages": [self._message_dict(message) for message in messages],
            "has_more": has_more,
            "purged_until": to_utc_isoformat(session.purged_until),
        }

    async def send_text(self, umo: str, text: str, client_id: str) -> dict[str, Any]:
        """Store a sending message and deliver it in the background.

        A repeated client_id returns the stored message without sending again.
        """
        writer = self._writer()
        if writer is not None and not writer.enabled:
            raise SnsServiceError("SNS client is disabled")
        if not text.strip():
            raise SnsServiceError("Message text is empty")
        # Only this method writes client_id, so the lock makes check-then-insert atomic.
        async with self._send_lock:
            existing = await self.db.get_im_message_by_client_id(client_id)
            if existing is not None:
                return self._message_dict(existing)
            if await self.db.get_im_session(umo) is None:
                raise SnsServiceError("Session not found", status_code=404)
            content = [{"type": "Plain", "text": text}]
            parsed = parse_umo(umo)
            [(message, session)] = await self.db.insert_im_messages(
                [
                    ImMessageWrite(
                        message=ImMessage(
                            umo=umo,
                            direction="out",
                            content=content,
                            source="webui",
                            client_id=client_id,
                            status="sending",
                        ),
                        platform_id=parsed["platform"],
                        message_type=parsed["message_type"],
                        preview=preview_text(content),
                    )
                ]
            )
        self._enqueue(
            "message.created", {"message": self._message_dict(message)}, session
        )
        task = asyncio.create_task(self._deliver(message, text))
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)
        return self._message_dict(message)

    def _remember_qq_group_scene(self, session: MessageSesion) -> None:
        """Let QQ official send to a group whose scene a restart has forgotten."""
        if session.message_type != MessageType.GROUP_MESSAGE:
            return
        for platform in self.core_lifecycle.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.id != session.platform_name:
                continue
            if meta.name not in ("qq_official", "qq_official_webhook"):
                return
            # Channels are GroupMessage too; a scene learned from inbound must stay.
            if session.session_id in getattr(platform, "_session_scene", {}):
                return
            remember = getattr(platform, "remember_session_scene", None)
            if callable(remember):
                remember(session.session_id, "group")
            return

    async def _deliver(self, message: ImMessage, text: str) -> None:
        assert message.id is not None
        status, error = "failed", None
        # This message is already stored, so Context.send_message must not record it.
        token = sns_send_suppressed.set(True)
        try:
            session = MessageSesion.from_str(message.umo)
            self._remember_qq_group_scene(session)
            # Typed text goes out as typed; QQ Official would render it as Markdown.
            chain = MessageChain([Plain(text)]).use_markdown(False)
            if await self.core_lifecycle.star_context.send_message(session, chain):
                status = "ok"
            else:
                error = f"Platform not running: {session.platform_name}"
        except Exception as exc:
            logger.warning(f"SNS send to {message.umo} failed: {exc!r}")
            error = (str(exc) or type(exc).__name__)[:500]
        finally:
            sns_send_suppressed.reset(token)
        try:
            updated = await self.db.update_im_message_status(message.id, status, error)
        except Exception:
            logger.exception("Failed to store an SNS send status.")
            return
        if updated is not None:
            self._enqueue("message.updated", {"message": self._message_dict(updated)})

    # Images

    async def open_image(self, message_id: int, index: int) -> SnsImage:
        """Resolve an image segment from its stored source only."""
        message = await self.db.get_im_message(message_id)
        segments = message.content if message is not None else []
        segment = segments[index] if 0 <= index < len(segments) else {}
        src = segment.get("src") if segment.get("type") == "Image" else None
        kind = src.get("kind") if isinstance(src, dict) else None
        value = str(src.get("value") or "") if isinstance(src, dict) else ""
        if kind == "file" and value:
            sent = message is not None and message.direction == "out"
            return await self._open_local_image(value, sent=sent)
        if kind == "url" and value:
            return await self._fetch_image(value)
        raise SnsServiceError("Image not available", status_code=404)

    @staticmethod
    async def _open_local_image(value: str, *, sent: bool) -> SnsImage:
        # Mirrors the roots the recorder accepts: temp, or its parent for sent images.
        root = Path(get_astrbot_temp_path()).resolve()
        root = root.parent if sent else root
        path = Path(value).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise SnsServiceError("Image not available", status_code=404)
        media_type = await detect_image_mime_type_async(path, default_mime_type=None)
        if not media_type:
            raise SnsServiceError("Image not available", status_code=404)
        return SnsImage(media_type=media_type, path=path)

    @staticmethod
    async def _require_public_host(url: str) -> None:
        """Reject image URLs that point into the local machine or a private network.

        The name is resolved again when connecting, so DNS rebinding is not covered.
        """
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise SnsServiceError("Image not available", status_code=404)
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.host, None)
        # Proxy tools in fake-ip mode answer every name from this benchmark range.
        fake_ip = ipaddress.ip_network("198.18.0.0/15")
        for info in infos:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            mapped = getattr(address, "ipv4_mapped", None)
            address = mapped or address
            if address.version == 4 and address in fake_ip:
                continue
            if not address.is_global:
                raise SnsServiceError("Image not available", status_code=404)

    async def _fetch_image(self, url: str) -> SnsImage:
        """Proxy an upstream image within 15 seconds and 20 MB in total."""
        max_bytes = 20 * 1024 * 1024
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        if self._http is None:
            # trust_env, the default, applies the process proxy settings.
            self._http = httpx.AsyncClient(timeout=15)
        client = self._http
        try:
            # Redirects are followed by hand so every hop passes the host check.
            for _ in range(4):
                await self._require_public_host(url)
                response = await asyncio.wait_for(
                    client.send(client.build_request("GET", url), stream=True),
                    timeout=15,
                )
                if not response.is_redirect or "location" not in response.headers:
                    break
                await response.aclose()
                url = str(response.url.join(response.headers["location"]))
            else:
                raise SnsServiceError("Image not available", status_code=404)
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, OSError) as exc:
            logger.warning(f"SNS image from {httpx.URL(url).host} failed: {exc!r}")
            raise SnsServiceError("Image not available", status_code=404) from exc
        declared = response.headers.get("content-length", "")
        body = response.aiter_bytes()
        media_type, first = None, b""
        if response.status_code == 200 and not (
            declared.isdigit() and int(declared) > max_bytes
        ):
            try:
                first = await asyncio.wait_for(anext(body, b""), deadline - loop.time())
            except (httpx.HTTPError, TimeoutError):
                first = b""
            # Image hosts often answer application/octet-stream, so the bytes decide.
            media_type = detect_image_mime_type(first, default_mime_type=None)
        if not media_type:
            await response.aclose()
            # Platform links expire, QQ's after about an hour with a 400, so a 4xx is routine.
            log = logger.debug if 400 <= response.status_code < 500 else logger.warning
            log(
                f"SNS image from {response.url.host} rejected: "
                f"status {response.status_code}, "
                f"type {response.headers.get('content-type')}, length {declared}"
            )
            raise SnsServiceError("Image not available", status_code=404)

        async def chunks() -> AsyncIterator[bytes]:
            received = len(first)
            try:
                yield first
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            anext(body), deadline - loop.time()
                        )
                    except StopAsyncIteration:
                        return
                    received += len(chunk)
                    # Raising aborts the connection, so a cut image is never cached.
                    if received > max_bytes:
                        raise SnsServiceError("Image exceeds 20 MB")
                    yield chunk
            finally:
                await response.aclose()

        return SnsImage(media_type=media_type, chunks=chunks(), close=response.aclose)
