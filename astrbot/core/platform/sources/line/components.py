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

import re
import sys
from typing import Any, ClassVar, Literal

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

    is_control_component: ClassVar[bool] = True

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


class LineFlexMedia(BaseModel):
    """Flex 里的一处媒体引用：由适配器负责物化、转码、尺寸收敛、上传、出公网 URL。

    Attributes:
        media: 标准 AstrBot Image 组件，来源不限（base64 / 本地路径 / 外链 / data URI）。
        profile: 目标位置的规格。
            image —— image 组件与 hero，收敛到 JPEG/PNG + 1024×1024 px 以内；
            icon —— icon 组件，另外压到更小的 payload；
            original —— 不转码、不缩放，产出的 URL 供 uri action 打开（客户端浏览器里
            看原图，不由 LINE 渲染，因此不受 Flex 的格式与像素约束）。
    """

    # 声明为 Any 而非 Image：与 LineQuickReplyItem.action 同样的理由 ——
    # pydantic v1 会按声明类型重新校验/复制字段，这里改为在转换时做 isinstance 检查。
    media: Any
    profile: Literal["image", "icon", "original"] = "image"

    class Config:
        arbitrary_types_allowed = True


class LineFlex(BaseMessageComponent):
    """Flex Message（实体组件）。

    contents 是 Flex 的 JSON 树，其结构正确性由插件负责 —— 适配器不校验、不改写，
    LINE 拒绝时会在日志里保留完整错误。

    需要发送非公网可达的图片（base64、本地文件、需要转码的外链）时，把 Image 放进
    media 映射，并在 contents 里用 LineFlex.ref(key) 占位；适配器在进入 LINE API 前
    把占位符替换成公网 URL。媒体对象不放进 contents，因此 contents 始终是可
    json.dumps 的纯 JSON —— 出错时能直接对照 LINE 的报错读 payload。

    Example:
        LineFlex(
            alt_text="Best 50",
            media={"hero": Comp.Image.fromBase64(png_b64)},
            contents={
                "type": "bubble",
                "hero": {"type": "image", "url": LineFlex.ref("hero"), "size": "full"},
            },
        )

    Flex 里的图受 1024×1024 px 硬限制，长图会被压得看不清。让用户点开看原图：同一张
    Image 挂两个 key，一个走默认 profile 进 hero，一个走 original 给 uri action。两个
    版本落到同一个文件时（源图本来就在 1024 以内）只会上传一次。

    Example:
        img = Comp.Image.fromBase64(png_b64)
        LineFlex(
            alt_text="Best 50",
            media={"hero": img, "full": LineFlexMedia(media=img, profile="original")},
            contents={
                "type": "bubble",
                "hero": {
                    "type": "image",
                    "url": LineFlex.ref("hero"),
                    "size": "full",
                    "action": {"type": "uri", "uri": LineFlex.ref("full")},
                },
            },
        )

    Attributes:
        alt_text: 无法渲染 Flex 时显示的文本。媒体解析失败时整条 Flex 会降级为它。
        contents: Flex JSON 树。
        media: key -> Image | LineFlexMedia。只有被 contents 引用到的 key 才会被处理。
    """

    type: str = "line_flex"  # type: ignore[assignment]
    alt_text: str
    contents: dict[str, Any]
    media: dict[str, Any] = {}

    MEDIA_SCHEME: ClassVar[str] = "astrbot-media://"
    MEDIA_KEY_PATTERN: ClassVar[re.Pattern] = re.compile(r"[A-Za-z0-9_-]+")
    """key 的合法字符集。

    限制字符集是为了让「整串是占位符」可判定：只按前缀判断的话，正文里一句恰好以
    astrbot-media:// 开头的文本会被误认成引用，然后因为查不到 key 而让整条 Flex 降级。
    """

    class Config:
        arbitrary_types_allowed = True

    @classmethod
    def ref(cls, key: str) -> str:
        """生成 contents 里的媒体占位符字符串。

        Args:
            key: media 映射里的键，只能由字母、数字、下划线、连字符组成。

        Returns:
            形如 "astrbot-media://hero" 的占位符；替换时按整个字符串精确匹配。

        Raises:
            ValueError: key 含非法字符。这种 key 永远匹配不上，静默产出一个不会被替换的
                占位符只会让 LINE 拒掉整批消息，不如在构造时就报错。
        """
        if not cls.MEDIA_KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"flex media key must match {cls.MEDIA_KEY_PATTERN.pattern}: {key!r}"
            )
        return f"{cls.MEDIA_SCHEME}{key}"

    @classmethod
    def parse_ref(cls, value: object) -> str | None:
        """整串是占位符时返回其 key，否则返回 None（普通 URL 与正文字符串走这条）。"""
        if not isinstance(value, str) or not value.startswith(cls.MEDIA_SCHEME):
            return None
        key = value[len(cls.MEDIA_SCHEME) :]
        return key if cls.MEDIA_KEY_PATTERN.fullmatch(key) else None

    def to_line_dict(self, contents: dict[str, Any] | None = None) -> dict[str, Any]:
        """产出 flex 消息对象。

        Args:
            contents: 已完成媒体替换的 JSON 树；None 表示原样使用 self.contents。
        """
        return {
            "type": "flex",
            "altText": self.alt_text,
            "contents": self.contents if contents is None else contents,
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
