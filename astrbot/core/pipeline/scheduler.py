from collections.abc import AsyncGenerator
from datetime import datetime

from astrbot.core import logger
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent
from astrbot.core.platform.sources.wecom_ai_bot.wecomai_event import (
    WecomAIBotMessageEvent,
)
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.utils.stat_recorders import session_activity_recorder

from .bootstrap import ensure_builtin_stages_registered
from .context import PipelineContext
from .stage import registered_stages
from .stage_order import STAGES_ORDER


class PipelineScheduler:
    """管道调度器，负责调度各个阶段的执行"""

    def __init__(self, context: PipelineContext) -> None:
        ensure_builtin_stages_registered()
        registered_stages.sort(
            key=lambda x: STAGES_ORDER.index(x.__name__),
        )  # 按照顺序排序
        self.ctx = context  # 上下文对象
        self.stages = []  # 存储阶段实例

    async def initialize(self) -> None:
        """初始化管道调度器时, 初始化所有阶段"""
        for stage_cls in registered_stages:
            stage_instance = stage_cls()  # 创建实例
            await stage_instance.initialize(self.ctx)
            self.stages.append(stage_instance)

    async def _process_stages(self, event: AstrMessageEvent, from_stage=0) -> None:
        """依次执行各个阶段

        Args:
            event (AstrMessageEvent): 事件对象
            from_stage (int): 从第几个阶段开始执行, 默认从0开始

        """
        for i in range(from_stage, len(self.stages)):
            stage = self.stages[i]  # 获取当前要执行的阶段
            # logger.debug(f"执行阶段 {stage.__class__.__name__}")
            coroutine = stage.process(
                event,
            )  # 调用阶段的process方法, 返回协程或者异步生成器

            if isinstance(coroutine, AsyncGenerator):
                # 如果返回的是异步生成器, 实现洋葱模型的核心
                async for _ in coroutine:
                    # 此处是前置处理完成后的暂停点(yield), 下面开始执行后续阶段
                    if event.is_stopped():
                        logger.debug(
                            f"Stage {stage.__class__.__name__} stopped event propagation.",
                        )
                        break

                    # 递归调用, 处理所有后续阶段
                    await self._process_stages(event, i + 1)

                    # 此处是后续所有阶段处理完毕后返回的点, 执行后置处理
                    if event.is_stopped():
                        logger.debug(
                            f"Stage {stage.__class__.__name__} stopped event propagation.",
                        )
                        break
            else:
                # 如果返回的是普通协程(不含yield的async函数), 则不进入下一层(基线条件)
                # 简单地等待它执行完成, 然后继续执行下一个阶段
                await coroutine

                if event.is_stopped():
                    logger.debug(
                        f"Stage {stage.__class__.__name__} stopped event propagation."
                    )
                    break

    async def execute(self, event: AstrMessageEvent) -> None:
        """执行 pipeline

        Args:
            event (AstrMessageEvent): 事件对象

        """
        active_event_registry.register(event)
        try:
            await self._process_stages(event)

            # 发送一个空消息, 以便于后续的处理
            if isinstance(event, WebChatMessageEvent | WecomAIBotMessageEvent):
                await event.send(None)

            logger.debug("pipeline execution completed.")
        finally:
            event.cleanup_temporary_local_files()
            active_event_registry.unregister(event)
            event._pipeline_finished.set()
            # 记录会话活跃统计（仅统计机器人实际处理并回复的消息）
            if event._has_send_oper:
                _record_session_activity(event)


def _record_session_activity(event: AstrMessageEvent) -> None:
    """把一次会话活跃合并进内存聚合器（同步、无 IO）。

    仅在机器人本次事件中有过发送操作时调用。名称字段为 best-effort：昵称直接读
    事件，群名仅读消息对象里适配器顺带填好的值，绝不为统计额外发起平台 API 请求。
    实际落库由 session_activity_recorder 的单个后台 worker 周期性批量完成。
    """
    try:
        user_id = event.get_sender_id()
        if not user_id:
            return
        group_obj = getattr(event.message_obj, "group", None)
        group_name = getattr(group_obj, "group_name", "") or ""
        session_activity_recorder.record(
            date=datetime.now().strftime("%Y-%m-%d"),
            platform_id=event.get_platform_id(),
            platform_type=event.platform_meta.name,
            message_type=event.get_message_type().value,
            group_id=event.get_group_id(),
            group_name=group_name,
            user_id=user_id,
            user_name=event.get_sender_name(),
        )
    except Exception as e:
        logger.error(f"记录会话活跃统计失败: {e}")
