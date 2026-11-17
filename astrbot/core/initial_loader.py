"""AstrBot 启动器，负责初始化和启动核心组件和仪表板服务器。

工作流程:
1. 初始化核心生命周期, 传递数据库和日志代理实例到核心生命周期
2. 运行核心生命周期任务和仪表板服务器
"""

import asyncio
import contextlib
import signal
import traceback
from collections.abc import Callable
from functools import partial

from astrbot.core import LogBroker, logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.dashboard.server import AstrBotDashboard


class InitialLoader:
    """AstrBot 启动器，负责初始化和启动核心组件和仪表板服务器。"""

    def __init__(self, db: BaseDatabase, log_broker: LogBroker) -> None:
        self.db = db
        self.logger = logger
        self.log_broker = log_broker
        self.webui_dir: str | None = None

    async def start(self) -> None:
        core_lifecycle = AstrBotCoreLifecycle(self.log_broker, self.db)

        try:
            await core_lifecycle.initialize()
        except Exception as e:
            logger.critical(traceback.format_exc())
            logger.critical(f"😭 Failed to initialize AstrBot: {e} !!!")
            return

        core_task = core_lifecycle.start()

        webui_dir = self.webui_dir

        self.dashboard_server = AstrBotDashboard(
            core_lifecycle,
            self.db,
            core_lifecycle.dashboard_shutdown_event,
            webui_dir,
        )

        coro = self.dashboard_server.run()
        dashboard_task = asyncio.ensure_future(coro) if coro else None
        if dashboard_task:
            # 启动核心任务和仪表板服务器
            task = asyncio.gather(core_task, asyncio.shield(dashboard_task))
        else:
            task = asyncio.ensure_future(core_task)

        stopping = False

        def request_shutdown(signame: str) -> None:
            # 关闭只走一次：后续信号不能打断正在进行的 stop()
            nonlocal stopping
            if stopping:
                logger.info(f"Received {signame}; shutdown is already in progress.")
                return
            stopping = True
            logger.info(f"Received {signame}.")
            task.cancel()

        loop = asyncio.get_running_loop()

        def notify(signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(request_shutdown, signal.Signals(signum).name)

        restores: list[Callable[[], object]] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, request_shutdown, sig.name)
            except NotImplementedError:
                # Windows event loops have no add_signal_handler.
                try:
                    signal.signal(sig, notify)
                except (OSError, ValueError) as exc:
                    logger.warning(f"Cannot handle {sig.name} for shutdown: {exc}")
                    continue
            except RuntimeError as exc:
                logger.warning(f"Cannot handle {sig.name} for shutdown: {exc}")
                continue
            else:
                restores.append(partial(loop.remove_signal_handler, sig))
            if previous is not None:
                restores.append(partial(signal.signal, sig, previous))

        try:
            await task  # 整个AstrBot在这里运行
        except asyncio.CancelledError:
            stopping = True
            logger.info("🌈 Shutting down AstrBot...")
            if dashboard_task:
                # 先触发 dashboard 优雅退出并等待其正常结束
                core_lifecycle.dashboard_shutdown_event.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await dashboard_task
            await core_lifecycle.stop()
            logger.info("AstrBot stopped.")
        finally:
            for undo in restores:
                undo()
