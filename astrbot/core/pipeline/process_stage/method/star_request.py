"""本地 Agent 模式的 AstrBot 插件调用 Stage"""

import re
import traceback
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.button_action import ButtonActionFilter
from astrbot.core.star.filter.callback import CallbackFilter
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.core.utils.stat_recorders import command_stat_recorder

from ...context import PipelineContext, call_event_hook, call_handler


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


def _command_args(handler: StarHandlerMetadata, message_str: str) -> str:
    """Return the text after the longest command name or alias the message starts with.

    message_str has its wake prefix stripped already; when no name matches (a
    handler before this one rewrote it), the whole text is returned.
    """
    text = re.sub(r"\s+", " ", (message_str or "").strip())
    for event_filter in handler.event_filters:
        if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
            for name in sorted(
                event_filter.get_complete_command_names(), key=len, reverse=True
            ):
                if text == name or text.startswith(f"{name} "):
                    return text[len(name) :].strip()
            break
    return text


def _log_trigger(event: AstrMessageEvent, action: str, plugin_name: str) -> None:
    """Log the command, regex or button handler about to run."""
    if event.is_private_chat():
        chat_tag = "[私聊]"
    else:
        group_id = event.get_group_id() or "unknown"
        group = getattr(event.message_obj, "group", None)
        group_name = getattr(group, "group_name", None) if group else None
        if group_name and str(group_name) not in {"", "N/A"}:
            chat_tag = f"[群聊 {group_name}({group_id})]"
        else:
            chat_tag = f"[群聊 ({group_id})]"
    sender_name = event.get_sender_name()
    sender_id = event.get_sender_id()
    sender = f"{sender_name}/{sender_id}" if sender_name else sender_id
    conf_name = event.get_extra("_inbound_log_conf_name", "unknown")
    # category lets the WebUI log page hide user chat.
    logger.info(
        f"[{conf_name}] [{event.get_platform_id()}({event.get_platform_name()})] "
        f"{chat_tag} {sender} -> {action.rstrip()} ({plugin_name})",
        extra={"category": "user_chat"},
    )


class StarRequestSubStage:
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
            # 指令 / 正则处理器打日志并计入触发统计（内存聚合，周期批量落库）；按钮回调只打日志
            trigger = _resolve_trigger(handler)
            if trigger:
                command_name, trigger_type = trigger
                # A regex may match secrets such as tokens, so only its handler is named.
                action = (
                    command_name
                    if trigger_type == "regex"
                    else f"/{command_name} "
                    f"{_command_args(handler, event.get_message_str())}"
                )
                _log_trigger(event, action, md.name or "")
                command_stat_recorder.record(
                    timestamp=datetime.now().replace(minute=0, second=0, microsecond=0),
                    platform_id=event.get_platform_id(),
                    trigger_type=trigger_type,
                    plugin_name=md.name or "",
                    command_name=command_name,
                )
            elif (
                any(
                    isinstance(event_filter, CallbackFilter | ButtonActionFilter)
                    for event_filter in handler.event_filters
                )
                and (label := event.describe_callback()) is not None
            ):
                _log_trigger(event, f"[按钮] {label}", md.name or "")

            # Match the component categories used by the plugin detail page.
            handler_category = (
                "command"
                if any(
                    isinstance(
                        event_filter, CommandFilter | CommandGroupFilter | RegexFilter
                    )
                    for event_filter in handler.event_filters
                )
                else "listener"
            )
            event.trace.record(
                "handler_start",
                kind="plugin_handler",
                status="running",
                plugin=md.name or handler.handler_module_path,
                plugin_display_name=md.display_name or md.name,
                handler=handler.handler_name,
                handler_full_name=handler.handler_full_name,
                handler_type=handler.event_type.name,
                handler_category=handler_category,
                priority=handler.extras_configs.get("priority", 0),
            )
            try:
                wrapper = call_handler(event, handler.handler, **params)
                async for ret in wrapper:
                    yield ret
                event.trace.record(
                    "handler_end",
                    kind="plugin_handler",
                    status="success",
                    plugin=md.name or handler.handler_module_path,
                    plugin_display_name=md.display_name or md.name,
                    handler=handler.handler_name,
                    handler_full_name=handler.handler_full_name,
                    handler_type=handler.event_type.name,
                    handler_category=handler_category,
                )
                if event.is_stopped():
                    break
                event.clear_result()  # 清除上一个 handler 的结果
            except Exception as e:
                traceback_text = traceback.format_exc()
                plugin_name = md.name or handler.handler_module_path
                # Keep handler failures on the plugin's side of the log filter.
                logger.error(
                    traceback_text,
                    extra={"plugin_name": plugin_name},
                )
                logger.error(
                    f"Star {handler.handler_full_name} handle error: {e}",
                    extra={"plugin_name": plugin_name},
                )
                event.trace.record(
                    "handler_end",
                    kind="plugin_handler",
                    status="error",
                    plugin=md.name or handler.handler_module_path,
                    plugin_display_name=md.display_name or md.name,
                    handler=handler.handler_name,
                    handler_full_name=handler.handler_full_name,
                    handler_type=handler.event_type.name,
                    handler_category=handler_category,
                    error=str(e),
                )

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
