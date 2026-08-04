"""LINE Messaging API 客户端。

发送结果是结构化的：调用方能拿到 HTTP 状态、sentMessages 的 id 与 quoteToken、
LINE 返回的 message 与 details。失败只按类别记日志 —— 不 push 兜底、不重试、
不剔除疑似非法对象后重发。
"""

import asyncio
import base64
import hmac
import json
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp

from astrbot.api import logger

from .line_media import (
    INBOUND_DOWNLOAD_LIMIT,
    MediaTooLargeError,
    inbound_temp_path,
    stream_response_to_file,
)

LINE_API_BASE = "https://api.line.me"
LINE_API_DATA_BASE = "https://api-data.line.me"

_RAW_BODY_LOG_LIMIT = 1024
"""失败日志里保留的原始正文长度上限（5xx 常返回 HTML 或网关文本）。"""


class LineErrorCategory(str, Enum):
    """发送失败的分类。兜底类保证没有落不进任何类别的状态码。"""

    REPLY_TOKEN_INVALID = "reply_token_invalid"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"


@dataclass(slots=True)
class LineSentMessage:
    """一条已发送消息的回执。quote_token 仅在该消息可作为引用目标时才有。"""

    id: str
    quote_token: str | None = None


@dataclass(slots=True)
class LineSendResult:
    """一次 reply / push 的完整结果。"""

    ok: bool
    status: int | None = None
    sent_messages: list[LineSentMessage] = field(default_factory=list)
    error_category: LineErrorCategory | None = None
    error_message: str = ""
    error_details: list[dict[str, Any]] = field(default_factory=list)
    raw_body: str = ""
    """截断后的原始正文；响应体解析失败时这是唯一的线索。"""


@dataclass(slots=True)
class LineMessageContent:
    """入站媒体的本地落盘结果。"""

    path: Path
    mime_type: str | None = None
    filename: str | None = None


class LineAPIClient:
    def __init__(
        self,
        *,
        channel_access_token: str,
        channel_secret: str,
        timeout_seconds: int = 30,
    ) -> None:
        self.channel_access_token = channel_access_token.strip()
        self.channel_secret = channel_secret.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("LINE API client is closed")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        self._closed = True
        if self._session and not self._session.closed:
            await self._session.close()

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        digest = hmac.new(
            self.channel_secret.encode("utf-8"),
            raw_body,
            sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature.strip())

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.channel_access_token}"}

    # ------------------------------------------------------------ 发送

    async def reply_message(
        self,
        reply_token: str,
        messages: list[dict[str, Any]],
        *,
        notification_disabled: bool = False,
    ) -> LineSendResult:
        """用 reply token 回复消息（一次最多 5 条）。"""
        payload = {
            "replyToken": reply_token,
            "messages": messages[:5],
            "notificationDisabled": notification_disabled,
        }
        return await self._send_messages(
            f"{LINE_API_BASE}/v2/bot/message/reply",
            payload=payload,
            op_name="reply",
        )

    async def push_message(
        self,
        to: str,
        messages: list[dict[str, Any]],
        *,
        notification_disabled: bool = False,
    ) -> LineSendResult:
        """主动推送消息（一次最多 5 条）。"""
        payload = {
            "to": to,
            "messages": messages[:5],
            "notificationDisabled": notification_disabled,
        }
        return await self._send_messages(
            f"{LINE_API_BASE}/v2/bot/message/push",
            payload=payload,
            op_name="push",
        )

    async def _send_messages(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        op_name: str,
    ) -> LineSendResult:
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                status = resp.status
        except Exception as e:
            result = LineSendResult(
                ok=False,
                error_category=LineErrorCategory.NETWORK_ERROR,
                error_message=str(e),
            )
            self._log_failure(op_name, result)
            return result

        data = self._parse_json_body(body)
        # 只有 2xx 算成功：3xx 不是「已送达」，当失败处理才不会静默丢消息。
        if 200 <= status < 300:
            return LineSendResult(
                ok=True,
                status=status,
                sent_messages=self._parse_sent_messages(data),
                raw_body="" if data is not None else body[:_RAW_BODY_LOG_LIMIT],
            )

        message = str(data.get("message", "")) if isinstance(data, dict) else ""
        details = data.get("details") if isinstance(data, dict) else None
        result = LineSendResult(
            ok=False,
            status=status,
            error_category=self._classify_error(status, message, details, op_name),
            error_message=message,
            error_details=[d for d in details if isinstance(d, dict)]
            if isinstance(details, list)
            else [],
            # 能解析出 JSON 时不再重复保留正文；解析失败时正文是唯一线索。
            raw_body="" if isinstance(data, dict) else body[:_RAW_BODY_LOG_LIMIT],
        )
        self._log_failure(op_name, result)
        return result

    @staticmethod
    def _parse_json_body(body: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(body)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _parse_sent_messages(data: dict[str, Any] | None) -> list[LineSentMessage]:
        if not isinstance(data, dict):
            return []
        sent = data.get("sentMessages")
        if not isinstance(sent, list):
            return []
        result: list[LineSentMessage] = []
        for item in sent:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id", "")).strip()
            if not message_id:
                continue
            quote_token = item.get("quoteToken")
            result.append(
                LineSentMessage(
                    id=message_id,
                    quote_token=str(quote_token) if quote_token else None,
                )
            )
        return result

    @staticmethod
    def _classify_error(
        status: int,
        message: str,
        details: Any,
        op_name: str = "",
    ) -> LineErrorCategory:
        """把失败归入一个类别。兜底类保证没有落不进任何类别的状态码。

        Args:
            status: HTTP 状态码。
            message: LINE 返回的 message。
            details: LINE 返回的 details。
            op_name: 操作类型；只有 reply 才可能是 reply token 失效。

        Returns:
            对应的失败类别。
        """
        if status == 400:
            haystack = message.lower()
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict):
                        haystack += " " + str(item.get("message", "")).lower()
                        haystack += " " + str(item.get("property", "")).lower()
            # 只有 reply 请求会有 reply token，且必须明确指向它 ——
            # 否则 quoteToken 之类的其它 token 错误会被误判成「token 失效」。
            if op_name == "reply" and "replytoken" in haystack.replace(" ", ""):
                return LineErrorCategory.REPLY_TOKEN_INVALID
            return LineErrorCategory.BAD_REQUEST
        if status in {401, 403}:
            return LineErrorCategory.UNAUTHORIZED
        if status == 429:
            return LineErrorCategory.RATE_LIMITED
        if 500 <= status < 600:
            return LineErrorCategory.SERVER_ERROR
        return LineErrorCategory.HTTP_ERROR

    @staticmethod
    def _log_failure(op_name: str, result: LineSendResult) -> None:
        logger.error(
            "[LINE] %s failed: category=%s status=%s message=%s details=%s body=%s",
            op_name,
            result.error_category.value if result.error_category else "unknown",
            result.status,
            result.error_message or "-",
            result.error_details or "-",
            result.raw_body or "-",
        )

    # ------------------------------------------------------------ 入站内容

    async def get_message_content(
        self,
        message_id: str,
        *,
        limit_bytes: int = INBOUND_DOWNLOAD_LIMIT,
        suffix: str = "",
        wait_transcoding: bool = True,
    ) -> LineMessageContent | None:
        """下载入站媒体到临时文件，超过 limit_bytes 即中止。

        Args:
            message_id: 入站消息 id。
            limit_bytes: 允许下载的最大字节数（各调用场景各自取值）。
            suffix: 落盘文件的后缀提示。
            wait_transcoding: 202 时是否轮询等待转码完成。

        Returns:
            落盘结果，或失败 / 超限时 None。
        """
        url = f"{LINE_API_DATA_BASE}/v2/bot/message/{message_id}/content"
        headers = self._auth_headers

        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await self._store_content(
                        resp, message_id, limit_bytes, suffix
                    )
                if resp.status != 202:
                    body = (await resp.text())[:_RAW_BODY_LOG_LIMIT]
                    logger.warning(
                        "[LINE] get content failed: message_id=%s status=%s body=%s",
                        message_id,
                        resp.status,
                        body,
                    )
                    return None

            if not wait_transcoding or not await self._wait_for_transcoding(message_id):
                return None
            # 转码完成后的第二次响应同样应用体积上限。
            async with session.get(url, headers=headers) as retry_resp:
                if retry_resp.status != 200:
                    body = (await retry_resp.text())[:_RAW_BODY_LOG_LIMIT]
                    logger.warning(
                        "[LINE] get content retry failed: message_id=%s status=%s body=%s",
                        message_id,
                        retry_resp.status,
                        body,
                    )
                    return None
                return await self._store_content(
                    retry_resp, message_id, limit_bytes, suffix
                )
        except MediaTooLargeError as e:
            logger.warning(
                "[LINE] inbound content aborted: message_id=%s %s", message_id, e
            )
            return None
        except Exception as e:
            logger.warning("[LINE] get content error: message_id=%s %s", message_id, e)
            return None

    async def _store_content(
        self,
        resp: aiohttp.ClientResponse,
        message_id: str,
        limit_bytes: int,
        suffix: str,
    ) -> LineMessageContent:
        dest = inbound_temp_path(message_id, suffix)
        await stream_response_to_file(resp, dest, limit_bytes)
        return LineMessageContent(
            path=dest,
            mime_type=resp.headers.get("Content-Type"),
            filename=self._extract_filename_from_disposition(
                resp.headers.get("Content-Disposition")
            ),
        )

    def _extract_filename_from_disposition(self, disposition: str | None) -> str | None:
        if not disposition:
            return None
        for part in disposition.split(";"):
            token = part.strip()
            if token.startswith("filename*="):
                val = token.split("=", 1)[1].strip().strip('"')
                if val.lower().startswith("utf-8''"):
                    val = val[7:]
                return unquote(val)
            if token.startswith("filename="):
                return token.split("=", 1)[1].strip().strip('"')
        return None

    async def _wait_for_transcoding(
        self,
        message_id: str,
        *,
        max_attempts: int = 10,
        interval_seconds: float = 1.0,
    ) -> bool:
        url = f"{LINE_API_DATA_BASE}/v2/bot/message/{message_id}/content/transcoding"
        headers = self._auth_headers

        for _ in range(max_attempts):
            try:
                session = await self._get_session()
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = self._parse_json_body(await resp.text())
                        status = str((data or {}).get("status", "")).lower()
                        if status == "succeeded":
                            return True
                        if status == "failed":
                            return False
            except Exception as e:
                logger.debug("[LINE] transcoding poll error: %s", e)
            await asyncio.sleep(interval_seconds)
        return False

    # ------------------------------------------------------------ 资料与体验

    async def get_user_display_name(self, user_id: str) -> str | None:
        """取 1:1 聊天中用户的显示名。"""
        return await self._get_display_name(f"{LINE_API_BASE}/v2/bot/profile/{user_id}")

    async def get_group_member_display_name(
        self, group_id: str, user_id: str
    ) -> str | None:
        """取群聊成员的显示名。"""
        return await self._get_display_name(
            f"{LINE_API_BASE}/v2/bot/group/{group_id}/member/{user_id}"
        )

    async def get_room_member_display_name(
        self, room_id: str, user_id: str
    ) -> str | None:
        """取多人聊天成员的显示名。"""
        return await self._get_display_name(
            f"{LINE_API_BASE}/v2/bot/room/{room_id}/member/{user_id}"
        )

    async def _get_display_name(self, url: str) -> str | None:
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers) as resp:
                if resp.status != 200:
                    logger.debug(
                        "[LINE] get profile failed: url=%s status=%s", url, resp.status
                    )
                    return None
                data = self._parse_json_body(await resp.text())
        except Exception as e:
            logger.debug("[LINE] get profile error: url=%s %s", url, e)
            return None
        display_name = str((data or {}).get("displayName", "")).strip()
        return display_name or None

    async def show_loading_animation(
        self,
        chat_id: str,
        loading_seconds: int = 20,
    ) -> bool:
        """在 1:1 聊天里显示 loading 动画。

        chatId 只接受用户 ID —— 群聊 / 多人聊天由调用方跳过，这里不做兼容假设。

        Args:
            chat_id: 目标用户 ID。
            loading_seconds: 显示秒数，5~60 且为 5 的倍数（会被规整到该范围）。

        Returns:
            请求是否被 LINE 受理。
        """
        seconds = min(max(int(loading_seconds), 5), 60)
        seconds = seconds - (seconds % 5)
        payload = {"chatId": chat_id, "loadingSeconds": seconds}
        try:
            session = await self._get_session()
            async with session.post(
                f"{LINE_API_BASE}/v2/bot/chat/loading/start",
                json=payload,
                headers={**self._auth_headers, "Content-Type": "application/json"},
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                body = (await resp.text())[:_RAW_BODY_LOG_LIMIT]
                logger.debug(
                    "[LINE] loading animation rejected: status=%s body=%s",
                    resp.status,
                    body,
                )
                return False
        except Exception as e:
            logger.debug("[LINE] loading animation error: %s", e)
            return False
