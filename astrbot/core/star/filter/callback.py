from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.button_interaction import BUTTON_CALLBACK_PREFIX

from . import HandlerFilter


class CallbackFilter(HandlerFilter):
    """匹配平台原生交互回调，可按回调数据的前缀筛选。"""

    def __init__(self, prefix: str | tuple[str, ...] | None = None) -> None:
        self.prefix = prefix

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """判断事件是否为匹配前缀的原生回调。

        Args:
            event: 入站事件。
            cfg: 生效的配置。

        Returns:
            事件是原生回调且回调数据以指定前缀开头时为 True。
        """
        if not event.is_callback_event() or event.is_button_interaction():
            return False
        data = event.get_callback_data() or ""
        # 通用按钮的令牌归 button_action 管，还原失败的令牌也不交给插件
        if data.startswith(BUTTON_CALLBACK_PREFIX):
            return False
        return self.prefix is None or data.startswith(self.prefix)
