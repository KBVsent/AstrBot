"""本地 Agent 模式的 AstrBot 插件调用 Stage"""

import traceback
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.core.utils.stat_recorders import command_stat_recorder

from ...context import PipelineContext, call_event_hook, call_handler
from ..stage import Stage


def _resolve_trigger(handler: StarHandlerMetadata) -> tuple[str, str] | None:
    """判断 handler 的触发方式并返回 (command_name, trigger_type)。

    指令用规范指令名，正则处理器用 handler 名（配合 plugin_name 唯一）。指令优先
    于正则（不依赖 event_filters 中过滤器的先后顺序）；非触发型过滤器（权限/消息
    类型等）忽略。返回 None 表示不计入统计。
    """
    regex_trigger: tuple[str, str] | None = None
    for event_filter in handler.event_filters:
        if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
            names = event_filter.get_complete_command_names()
            return (names[0], "command") if names else None
        if isinstance(event_filter, RegexFilter) and regex_trigger is None:
            regex_trigger = (handler.handler_name, "regex")
    return regex_trigger


class StarRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.prompt_prefix = ctx.astrbot_config["provider_settings"]["prompt_prefix"]
        self.identifier = ctx.astrbot_config["provider_settings"]["identifier"]
        self.ctx = ctx

    async def process(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        activated_handlers: list[StarHandlerMetadata] = event.get_extra(
            "activated_handlers",
        )
        handlers_parsed_params: dict[str, dict[str, Any]] = event.get_extra(
            "handlers_parsed_params",
        )
        if not handlers_parsed_params:
            handlers_parsed_params = {}

        for handler in activated_handlers:
            if event.is_stopped():
                break
            params = handlers_parsed_params.get(handler.handler_full_name, {})
            md = star_map.get(handler.handler_module_path)
            if not md:
                logger.warning(
                    f"Cannot find plugin for given handler module path: {handler.handler_module_path}",
                )
                continue
            logger.debug(f"plugin -> {md.name} - {handler.handler_name}")

            # 统计指令 / 正则处理器触发次数（合并进内存聚合器，周期批量落库）
            trigger = _resolve_trigger(handler)
            if trigger:
                command_name, trigger_type = trigger
                command_stat_recorder.record(
                    timestamp=datetime.now().replace(minute=0, second=0, microsecond=0),
                    platform_id=event.get_platform_id(),
                    trigger_type=trigger_type,
                    plugin_name=md.name or "",
                    command_name=command_name,
                )

            try:
                wrapper = call_handler(event, handler.handler, **params)
                async for ret in wrapper:
                    yield ret
                if event.is_stopped():
                    break
                event.clear_result()  # 清除上一个 handler 的结果
            except Exception as e:
                traceback_text = traceback.format_exc()
                logger.error(traceback_text)
                logger.error(f"Star {handler.handler_full_name} handle error: {e}")

                await call_event_hook(
                    event,
                    EventType.OnPluginErrorEvent,
                    md.name,
                    handler.handler_name,
                    e,
                    traceback_text,
                )

                if not event.is_stopped() and event.is_at_or_wake_command:
                    ret = f":(\n\n在调用插件 {md.name} 的处理函数 {handler.handler_name} 时出现异常：{e}"
                    event.set_result(MessageEventResult().message(ret))
                    yield
                    event.clear_result()

                event.stop_event()
