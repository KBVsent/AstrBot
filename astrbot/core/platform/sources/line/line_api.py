"""LINE Messaging API 客户端。

发送结果是结构化的：调用方能拿到 HTTP 状态、sentMessages 的 id 与 quoteToken、
LINE 返回的 message 与 details。失败只按类别记日志 —— 不 push 兜底、不重试、
不剔除疑似非法对象后重发。
"""

import asyncio
import base64
import hmac
import json
import logging
import re
from collections.abc import Sequence
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

# Rich Menu 的硬约束。这些值在本地先拦一道：违反它们的请求必然 400，
# 而 Rich Menu 的调用方通常在一个 reconcile 循环里，白打的往返会被放大。
_RICH_MENU_NAME_MAX = 300
_RICH_MENU_CHAT_BAR_TEXT_MAX = 14
_RICH_MENU_AREA_MAX = 20
_RICH_MENU_IMAGE_MAX_BYTES = 1024 * 1024
_RICH_MENU_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg"})
_RICH_MENU_ALIAS_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_RICH_MENU_BULK_MAX = 500
"""bulk link / unlink 单次接受的 userId 上限，超出由客户端切片。"""


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
class LineApiResult:
    """一次非发送类 API 调用的结果（Rich Menu 等）。

    与 LineSendResult 分开：那个类的 sent_messages 在这里没有意义，而这里需要一个
    通用的 data 承载响应体（richMenuId / richmenus 列表 / alias 信息）。
    """

    ok: bool
    status: int | None = None
    data: dict[str, Any] | None = None
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
    def _log_failure(
        op_name: str,
        result: LineSendResult | LineApiResult,
        *,
        level: int = logging.ERROR,
    ) -> None:
        logger.log(
            level,
            "[LINE] %s failed: category=%s status=%s message=%s details=%s body=%s",
            op_name,
            result.error_category.value if result.error_category else "unknown",
            result.status,
            result.error_message or "-",
            result.error_details or "-",
            result.raw_body or "-",
        )

    # ------------------------------------------------------------ 通用请求

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        content_type: str | None = None,
        op_name: str,
        expected_absent: frozenset[int] = frozenset(),
    ) -> LineApiResult:
        """发一次请求并把结果规整成 LineApiResult。

        与 _send_messages 分开：那个方法要解析 sentMessages、只支持 POST JSON，
        而这里要覆盖 GET / DELETE / POST JSON / POST 二进制四种形态。

        Args:
            json_body: JSON 请求体；与 body 互斥。
            body: 二进制请求体（上传图片），需同时给 content_type。
            op_name: 失败日志里的操作名。
            expected_absent: 视为「正常的查不到」的状态码（通常是 404）。命中时仍返回
                ok=False，但只记 debug —— 查一个没绑定过的用户是常态，不该刷 error。
        """
        headers = dict(self._auth_headers)
        if content_type:
            headers["Content-Type"] = content_type
        elif json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            session = await self._get_session()
            async with session.request(
                method,
                url,
                json=json_body,
                data=body,
                headers=headers,
            ) as resp:
                raw = await resp.text()
                status = resp.status
        except Exception as e:
            result = LineApiResult(
                ok=False,
                error_category=LineErrorCategory.NETWORK_ERROR,
                error_message=str(e),
            )
            self._log_failure(op_name, result)
            return result

        data = self._parse_json_body(raw)
        # 与发送路径同口径：只有 2xx 算成功。bulk link / unlink 返回 202。
        if 200 <= status < 300:
            return LineApiResult(
                ok=True,
                status=status,
                data=data,
                raw_body="" if data is not None else raw[:_RAW_BODY_LOG_LIMIT],
            )

        message = str(data.get("message", "")) if isinstance(data, dict) else ""
        details = data.get("details") if isinstance(data, dict) else None
        result = LineApiResult(
            ok=False,
            status=status,
            error_category=self._classify_error(status, message, details, op_name),
            error_message=message,
            error_details=[d for d in details if isinstance(d, dict)]
            if isinstance(details, list)
            else [],
            raw_body="" if isinstance(data, dict) else raw[:_RAW_BODY_LOG_LIMIT],
        )
        self._log_failure(
            op_name,
            result,
            level=logging.DEBUG if status in expected_absent else logging.ERROR,
        )
        return result

    @staticmethod
    def _reject(op_name: str, reason: str) -> LineApiResult:
        """本地校验不通过时的统一出口：不发请求，直接失败。

        记 error 而不是 warning：这类问题只可能来自调用方拼错对象，不是运行时波动，
        必须在日志里显眼到能被立刻发现。
        """
        logger.error("[LINE] %s rejected locally: %s", op_name, reason)
        return LineApiResult(
            ok=False,
            error_category=LineErrorCategory.BAD_REQUEST,
            error_message=reason,
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

    async def get_user_language(self, user_id: str) -> str | None:
        """取用户的界面语言（BCP 47，如 ja / zh-Hant）。取不到返回 None。

        /v2/bot/profile/{userId} 是账号级端点，只认该 userId 是不是本官方账号的
        follower，与消息来自 1:1 还是群聊无关；群聊与多人聊天的 member profile 端点
        没有 language，所以这里不试那两个。

        取不到是常态，且有两种：404 表示不是 follower（未加好友或已拉黑），
        200 但响应无 language 键表示已是 follower 但用户未同意 LINE 隐私政策。
        """
        data = await self._get_profile(f"{LINE_API_BASE}/v2/bot/profile/{user_id}")
        language = str((data or {}).get("language", "")).strip()
        return language or None

    async def _get_display_name(self, url: str) -> str | None:
        data = await self._get_profile(url)
        display_name = str((data or {}).get("displayName", "")).strip()
        return display_name or None

    async def _get_profile(self, url: str) -> dict | None:
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers) as resp:
                if resp.status != 200:
                    logger.debug(
                        "[LINE] get profile failed: url=%s status=%s", url, resp.status
                    )
                    return None
                return self._parse_json_body(await resp.text())
        except Exception as e:
            logger.debug("[LINE] get profile error: url=%s %s", url, e)
            return None

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

    # ------------------------------------------------------------ Rich Menu

    async def create_rich_menu(self, menu: dict[str, Any]) -> str | None:
        """创建一张 Rich Menu，返回 richMenuId；失败返回 None。

        创建出来的菜单此时还没有图，客户端上是一片空白，必须紧接着
        upload_rich_menu_image()，否则不要把它设为默认或绑给用户。
        """
        reason = self._invalid_rich_menu_reason(menu)
        if reason:
            self._reject("create rich menu", reason)
            return None
        result = await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/richmenu",
            json_body=menu,
            op_name="create rich menu",
        )
        rich_menu_id = str((result.data or {}).get("richMenuId", "")).strip()
        return rich_menu_id or None

    async def validate_rich_menu(self, menu: dict[str, Any]) -> LineApiResult:
        """干跑校验一个 Rich Menu 对象，不创建任何东西。

        返回完整结果而不是 bool：这个接口存在的意义就是拿到「哪里不合规」。
        """
        reason = self._invalid_rich_menu_reason(menu)
        if reason:
            return self._reject("validate rich menu", reason)
        return await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/richmenu/validate",
            json_body=menu,
            op_name="validate rich menu",
        )

    async def upload_rich_menu_image(
        self,
        rich_menu_id: str,
        image: bytes,
        *,
        content_type: str = "image/png",
    ) -> LineApiResult:
        """上传菜单图。走 api-data 域，请求体是裸二进制而非 multipart。"""
        if content_type not in _RICH_MENU_IMAGE_CONTENT_TYPES:
            return self._reject(
                "upload rich menu image",
                f"content_type must be one of "
                f"{sorted(_RICH_MENU_IMAGE_CONTENT_TYPES)}, got {content_type}",
            )
        if not image:
            return self._reject("upload rich menu image", "image is empty")
        if len(image) > _RICH_MENU_IMAGE_MAX_BYTES:
            return self._reject(
                "upload rich menu image",
                f"image is {len(image)} bytes, over the "
                f"{_RICH_MENU_IMAGE_MAX_BYTES} bytes limit",
            )
        return await self._request(
            "POST",
            f"{LINE_API_DATA_BASE}/v2/bot/richmenu/{rich_menu_id}/content",
            body=image,
            content_type=content_type,
            op_name="upload rich menu image",
        )

    async def get_rich_menu(self, rich_menu_id: str) -> dict[str, Any] | None:
        """取单张菜单的定义；不存在返回 None。"""
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/richmenu/{rich_menu_id}",
            op_name="get rich menu",
            expected_absent=frozenset({404}),
        )
        return result.data if result.ok else None

    async def list_rich_menus(self) -> list[dict[str, Any]]:
        """列出本 channel 用 Messaging API 建的全部菜单。

        注意范围：LINE Official Account Manager 里手工配的菜单**不在结果里**，
        两套工具管理的是彼此不可见的实例。
        """
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/richmenu/list",
            op_name="list rich menus",
        )
        menus = (result.data or {}).get("richmenus")
        return (
            [m for m in menus if isinstance(m, dict)] if isinstance(menus, list) else []
        )

    async def delete_rich_menu(self, rich_menu_id: str) -> LineApiResult:
        """删除一张菜单。正被引用的 alias 会一并失效，删之前先把 alias 指向新菜单。"""
        return await self._request(
            "DELETE",
            f"{LINE_API_BASE}/v2/bot/richmenu/{rich_menu_id}",
            op_name="delete rich menu",
        )

    # ---- 默认菜单 ----

    async def set_default_rich_menu(self, rich_menu_id: str) -> LineApiResult:
        """设为默认菜单（所有没有 per-user 绑定的用户看到它）。

        它的优先级高于 LINE Official Account Manager 里配的默认菜单，会把那张盖掉。
        """
        return await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/user/all/richmenu/{rich_menu_id}",
            op_name="set default rich menu",
        )

    async def get_default_rich_menu_id(self) -> str | None:
        """取当前默认菜单 id；没设过返回 None。"""
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/user/all/richmenu",
            op_name="get default rich menu",
            expected_absent=frozenset({404}),
        )
        return str((result.data or {}).get("richMenuId", "")).strip() or None

    async def cancel_default_rich_menu(self) -> LineApiResult:
        """撤销默认菜单。"""
        return await self._request(
            "DELETE",
            f"{LINE_API_BASE}/v2/bot/user/all/richmenu",
            op_name="cancel default rich menu",
        )

    # ---- per-user 绑定 ----

    async def link_rich_menu_to_user(
        self, user_id: str, rich_menu_id: str
    ) -> LineApiResult:
        """把菜单绑给单个用户。只能绑用户，群 / 多人聊天绑不了。

        返回完整结果而不是 bool：调用方需要区分「这人不是好友了」（4xx，该清掉本地
        绑定缓存）和「网络抖了一下」（该重试）。
        """
        return await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/user/{user_id}/richmenu/{rich_menu_id}",
            op_name="link rich menu to user",
        )

    async def unlink_rich_menu_from_user(self, user_id: str) -> LineApiResult:
        """解除单个用户的绑定，之后他看到的是默认菜单。"""
        return await self._request(
            "DELETE",
            f"{LINE_API_BASE}/v2/bot/user/{user_id}/richmenu",
            op_name="unlink rich menu from user",
        )

    async def get_rich_menu_id_of_user(self, user_id: str) -> str | None:
        """取用户当前绑定的菜单 id；未绑定返回 None（404 是常态，不记 error）。"""
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/user/{user_id}/richmenu",
            op_name="get rich menu of user",
            expected_absent=frozenset({404}),
        )
        return str((result.data or {}).get("richMenuId", "")).strip() or None

    async def bulk_link_rich_menu(
        self, rich_menu_id: str, user_ids: Sequence[str]
    ) -> LineApiResult:
        """批量绑定。超过 500 个 userId 时自动切片，调用方不必关心这个上限。

        LINE 侧是异步受理（202），返回 ok 只代表请求被接受，不代表全部生效。
        """
        return await self._bulk_rich_menu(
            f"{LINE_API_BASE}/v2/bot/richmenu/bulk/link",
            user_ids,
            extra={"richMenuId": rich_menu_id},
            op_name="bulk link rich menu",
        )

    async def bulk_unlink_rich_menu(self, user_ids: Sequence[str]) -> LineApiResult:
        """批量解绑，切片规则同 bulk_link_rich_menu。"""
        return await self._bulk_rich_menu(
            f"{LINE_API_BASE}/v2/bot/richmenu/bulk/unlink",
            user_ids,
            extra={},
            op_name="bulk unlink rich menu",
        )

    async def _bulk_rich_menu(
        self,
        url: str,
        user_ids: Sequence[str],
        *,
        extra: dict[str, Any],
        op_name: str,
    ) -> LineApiResult:
        """按 500 一批发出去。任一批失败即整体失败，但剩余批次照发。

        不在首个失败处中断：这些调用彼此独立，中断只会让「已绑一半」的状态更难收敛，
        而下一轮 reconcile 本来就会重试失败的那些人。
        """
        ids = [str(uid).strip() for uid in user_ids if str(uid).strip()]
        if not ids:
            return self._reject(op_name, "user_ids is empty")

        failure: LineApiResult | None = None
        last: LineApiResult | None = None
        for start in range(0, len(ids), _RICH_MENU_BULK_MAX):
            chunk = ids[start : start + _RICH_MENU_BULK_MAX]
            last = await self._request(
                "POST",
                url,
                json_body={**extra, "userIds": chunk},
                op_name=op_name,
            )
            if not last.ok and failure is None:
                failure = last
        return failure or last  # type: ignore[return-value]  # ids 非空，循环必然跑过

    # ---- 别名（richmenuswitch 的切换目标） ----

    async def create_rich_menu_alias(
        self, alias_id: str, rich_menu_id: str
    ) -> LineApiResult:
        """创建别名。richmenuswitch action 只认别名，不认 richMenuId。"""
        if not _RICH_MENU_ALIAS_ID_PATTERN.match(alias_id):
            return self._reject(
                "create rich menu alias",
                f"alias id must match {_RICH_MENU_ALIAS_ID_PATTERN.pattern}, "
                f"got {alias_id!r}",
            )
        return await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/richmenu/alias",
            json_body={"richMenuAliasId": alias_id, "richMenuId": rich_menu_id},
            op_name="create rich menu alias",
        )

    async def update_rich_menu_alias(
        self, alias_id: str, rich_menu_id: str
    ) -> LineApiResult:
        """把已有别名改指到另一张菜单。菜单没有「更新」接口，改版就是靠这一步切换。"""
        return await self._request(
            "POST",
            f"{LINE_API_BASE}/v2/bot/richmenu/alias/{alias_id}",
            json_body={"richMenuId": rich_menu_id},
            op_name="update rich menu alias",
        )

    async def delete_rich_menu_alias(self, alias_id: str) -> LineApiResult:
        """删除别名。"""
        return await self._request(
            "DELETE",
            f"{LINE_API_BASE}/v2/bot/richmenu/alias/{alias_id}",
            op_name="delete rich menu alias",
        )

    async def get_rich_menu_alias(self, alias_id: str) -> dict[str, Any] | None:
        """取别名信息；不存在返回 None。"""
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/richmenu/alias/{alias_id}",
            op_name="get rich menu alias",
            expected_absent=frozenset({404}),
        )
        return result.data if result.ok else None

    async def list_rich_menu_aliases(self) -> list[dict[str, Any]]:
        """列出本 channel 的全部别名。"""
        result = await self._request(
            "GET",
            f"{LINE_API_BASE}/v2/bot/richmenu/alias/list",
            op_name="list rich menu aliases",
        )
        aliases = (result.data or {}).get("aliases")
        return (
            [a for a in aliases if isinstance(a, dict)]
            if isinstance(aliases, list)
            else []
        )

    @staticmethod
    def _invalid_rich_menu_reason(menu: Any) -> str:
        """返回第一条不合规的原因；合规返回空串。

        只查 LINE 明文规定、且违反后必然 400 的那几条硬上限。尺寸、坐标、action 的
        合法性交给服务端 —— 在本地重实现一份校验只会与 LINE 的规则漂移。
        """
        if not isinstance(menu, dict):
            return f"rich menu must be a dict, got {type(menu).__name__}"

        name = str(menu.get("name", ""))
        if len(name) > _RICH_MENU_NAME_MAX:
            return f"name is {len(name)} chars, over the {_RICH_MENU_NAME_MAX} limit"

        chat_bar_text = str(menu.get("chatBarText", ""))
        if len(chat_bar_text) > _RICH_MENU_CHAT_BAR_TEXT_MAX:
            return (
                f"chatBarText is {len(chat_bar_text)} chars, over the "
                f"{_RICH_MENU_CHAT_BAR_TEXT_MAX} limit"
            )

        areas = menu.get("areas")
        if not isinstance(areas, list):
            return "areas must be a list"
        if len(areas) > _RICH_MENU_AREA_MAX:
            return f"areas has {len(areas)} items, over the {_RICH_MENU_AREA_MAX} limit"

        return ""
