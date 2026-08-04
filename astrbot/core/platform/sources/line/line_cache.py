"""LINE 引用凭据与被引用内容的进程内缓存。

两张表，键都必须带聊天作用域：quoteToken 只能在取得它的那个聊天里使用，跨聊天使用是
非法对象，会让整个 reply 批次 400 —— 这个判断不能交给 LINE 拒绝。

- token 表：(chat_id, message_id) -> quoteToken。quoteToken 官方明确不会过期、
  可重复使用，因此不做 TTL 驱逐，但仍需容量上限，否则会变成无界字典。
- 内容表：(chat_id, message_id) -> 消息组件，用于恢复被引用消息的内容。
  定位是尽力恢复：有限的时间窗与容量，可调，非承诺。
"""

from __future__ import annotations

import time
from collections import OrderedDict

from astrbot.core.message.components import BaseMessageComponent

DEFAULT_TOKEN_CAPACITY = 2000
DEFAULT_CONTENT_CAPACITY = 500
DEFAULT_CONTENT_TTL_SECONDS = 1800


class LineQuoteStore:
    """引用凭据与被引用内容的 LRU 缓存。"""

    def __init__(
        self,
        *,
        token_capacity: int = DEFAULT_TOKEN_CAPACITY,
        content_capacity: int = DEFAULT_CONTENT_CAPACITY,
        content_ttl_seconds: float = DEFAULT_CONTENT_TTL_SECONDS,
    ) -> None:
        self._tokens: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._contents: OrderedDict[
            tuple[str, str], tuple[float, list[BaseMessageComponent]]
        ] = OrderedDict()
        self._token_capacity = token_capacity
        self._content_capacity = content_capacity
        self._content_ttl = content_ttl_seconds

    def put_token(self, chat_id: str, message_id: str, quote_token: str | None) -> None:
        """记录一条消息的 quoteToken。

        写入必须容忍 quoteToken 缺失：入站 Audio / File / Location 没有该字段，
        出站响应也只在「所发消息可作为引用目标」时才返回。
        """
        if not chat_id or not message_id or not quote_token:
            return
        key = (chat_id, message_id)
        self._tokens.pop(key, None)
        self._tokens[key] = quote_token
        while len(self._tokens) > self._token_capacity:
            self._tokens.popitem(last=False)

    def get_token(self, chat_id: str, message_id: str) -> str | None:
        """取某聊天中某条消息的 quoteToken；跨聊天或未记录时返回 None。"""
        key = (chat_id, message_id)
        token = self._tokens.get(key)
        if token is not None:
            self._tokens.move_to_end(key)
        return token

    def put_content(
        self,
        chat_id: str,
        message_id: str,
        components: list[BaseMessageComponent],
    ) -> None:
        """缓存一条消息的内容组件，供之后恢复被引用消息。"""
        if not chat_id or not message_id or not components:
            return
        key = (chat_id, message_id)
        self._contents.pop(key, None)
        self._contents[key] = (time.time(), list(components))
        self._evict_contents()

    def get_content(
        self, chat_id: str, message_id: str
    ) -> list[BaseMessageComponent] | None:
        """取缓存的被引用消息内容；未命中或已过窗口时返回 None。"""
        self._evict_contents()
        key = (chat_id, message_id)
        entry = self._contents.get(key)
        if entry is None:
            return None
        self._contents.move_to_end(key)
        return list(entry[1])

    def _evict_contents(self) -> None:
        now = time.time()
        expired = [
            key
            for key, (created_at, _) in self._contents.items()
            if now - created_at > self._content_ttl
        ]
        for key in expired:
            self._contents.pop(key, None)
        while len(self._contents) > self._content_capacity:
            self._contents.popitem(last=False)
