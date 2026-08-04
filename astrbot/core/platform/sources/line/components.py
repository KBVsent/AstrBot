"""LINE 平台专用消息组件。

插件把这些组件放进 MessageChain 即可表达 LINE 原生能力。分两类：

- 实体组件 —— 产出一个消息对象、占用「一次请求最多 5 条」的配额：
  LineFlex、LineRawMessage。
- 控制组件 —— 不产出消息对象，只修饰最终发送批次，不占配额：LineQuickReply。
  引用回复用 AstrBot 标准的 Reply(id=...)，没有 LINE 专用组件。
  一个 pipeline 可能多次 send()，控制组件会累积到最终发送时才消费；
  给了多个则取最后一个并 warning。

Action 的可用位置由 LINE 规定，spec 表达不了：camera / cameraRoll 仅 Quick Reply
可用；postback / uri 可用于 Flex、Quick Reply、Template、Rich Menu。Imagemap 用的是
另一套 action 类型，本适配器不产出 imagemap 消息。因此本模块的 action 只出现在 Quick Reply
里，以及插件自备的 Flex JSON 里（后者结构由插件负责，适配器不校验）。
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from astrbot.api import logger
from astrbot.core.message.components import BaseMessageComponent

if sys.version_info >= (3, 14):
    from pydantic import BaseModel
else:
    from pydantic.v1 import BaseModel

POSTBACK_DATA_MAX_LENGTH = 300
QUICK_REPLY_MAX_ITEMS = 13


class LineAction(BaseModel):
    """LINE action 基类。子类实现 to_line_dict。"""

    label: str | None = None

    def to_line_dict(self) -> dict[str, Any] | None:
        raise NotImplementedError


class LinePostbackAction(LineAction):
    """Postback action：点击后 bot 收到 PostbackEvent，不产生用户可见的发言。

    Attributes:
        data: 回传数据，最长 300 字符（超长的对象会让整批消息被 LINE 拒绝，故超长即丢弃）。
        display_text: 点击后代替用户说出的文本。
        input_option: closeRichMenu / openRichMenu / openKeyboard / openVoice。
        fill_in_text: input_option="openKeyboard" 时预填进输入框的文本。
    """

    data: str
    display_text: str | None = None
    input_option: (
        Literal["closeRichMenu", "openRichMenu", "openKeyboard", "openVoice"] | None
    ) = None
    fill_in_text: str | None = None

    def to_line_dict(self) -> dict[str, Any] | None:
        if len(self.data) > POSTBACK_DATA_MAX_LENGTH:
            logger.warning(
                "[LINE] postback data exceeds %s chars, action skipped.",
                POSTBACK_DATA_MAX_LENGTH,
            )
            return None
        action: dict[str, Any] = {"type": "postback", "data": self.data}
        if self.label:
            action["label"] = self.label
        if self.display_text:
            action["displayText"] = self.display_text
        if self.input_option:
            action["inputOption"] = self.input_option
        if self.fill_in_text is not None:
            action["fillInText"] = self.fill_in_text
        return action


class LineUriAction(LineAction):
    """URI action：点击后在客户端打开链接（含 LIFF 应用的 URL）。"""

    uri: str

    def to_line_dict(self) -> dict[str, Any] | None:
        if not self.uri:
            return None
        action: dict[str, Any] = {"type": "uri", "uri": self.uri}
        if self.label:
            action["label"] = self.label
        return action


class LineCameraAction(LineAction):
    """打开相机。仅 Quick Reply 可用。"""

    def to_line_dict(self) -> dict[str, Any] | None:
        action: dict[str, Any] = {"type": "camera"}
        if self.label:
            action["label"] = self.label
        return action


class LineCameraRollAction(LineAction):
    """打开相册。仅 Quick Reply 可用。"""

    def to_line_dict(self) -> dict[str, Any] | None:
        action: dict[str, Any] = {"type": "cameraRoll"}
        if self.label:
            action["label"] = self.label
        return action


class LineQuickReplyItem(BaseModel):
    """Quick Reply 的一个按钮。

    Attributes:
        action: 点击后的行为，任一 LineAction 子类实例。
        image_url: 按钮图标，必须是 PNG、1:1、≤ 1 MB 的 HTTPS URL（由插件保证）。
    """

    # 声明为 Any 而非 LineAction：pydantic v1 会把子类实例按声明类型重新校验，
    # 从而把 data / uri 等子类字段丢掉。这里改为在转换时做 isinstance 检查。
    action: Any
    image_url: str | None = None

    class Config:
        arbitrary_types_allowed = True

    def to_line_dict(self) -> dict[str, Any] | None:
        if not isinstance(self.action, LineAction):
            logger.warning(
                "[LINE] quick reply item action is not a LineAction, skipped: %r",
                self.action,
            )
            return None
        action = self.action.to_line_dict()
        if not action:
            return None
        item: dict[str, Any] = {"type": "action", "action": action}
        if self.image_url:
            item["imageUrl"] = self.image_url
        return item


class LineQuickReply(BaseMessageComponent):
    """Quick Reply（控制组件）：显示在对话框底部，不占用 5 条消息配额。

    附着到最终批次的最后一条消息上；批次里没有实体消息时整体丢弃并 warning。
    items 上限 13，超出部分丢弃。
    """

    type: str = "line_quick_reply"  # type: ignore[assignment]
    items: list[LineQuickReplyItem] = []

    class Config:
        arbitrary_types_allowed = True

    def to_line_dict(self) -> dict[str, Any] | None:
        items: list[dict[str, Any]] = []
        for item in self.items:
            payload = item.to_line_dict()
            if payload:
                items.append(payload)
        if not items:
            return None
        if len(items) > QUICK_REPLY_MAX_ITEMS:
            logger.warning(
                "[LINE] quick reply items exceed %s, extra item(s) dropped.",
                QUICK_REPLY_MAX_ITEMS,
            )
            items = items[:QUICK_REPLY_MAX_ITEMS]
        return {"items": items}


class LineFlex(BaseMessageComponent):
    """Flex Message（实体组件）。

    contents 是 Flex 的 JSON 树，其结构正确性由插件负责 —— 适配器不校验、不改写，
    LINE 拒绝时会在日志里保留完整错误。
    """

    type: str = "line_flex"  # type: ignore[assignment]
    alt_text: str
    contents: dict[str, Any]

    def to_line_dict(self) -> dict[str, Any]:
        return {
            "type": "flex",
            "altText": self.alt_text,
            "contents": self.contents,
        }


class LineRawMessage(BaseMessageComponent):
    """原始消息对象直通（实体组件）。

    高级、非安全逃生口：message 原样进入 messages 数组，适配器不改写、不校验。
    payload 不合法会让整批消息一起发不出去，正确性完全由调用者负责。
    """

    type: str = "line_raw"  # type: ignore[assignment]
    message: dict[str, Any]

    def to_line_dict(self) -> dict[str, Any]:
        return self.message
