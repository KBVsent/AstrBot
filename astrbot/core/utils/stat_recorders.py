"""统计类写入的通用内存聚合器 + 具体实例。

把"每条事件一个后台 task + 一个独立写事务"的热点写入，改为按业务键在内存中
合并、由单个后台 worker 周期性批量 UPSERT，缓解 SQLite 单写者锁竞争。

:class:`PeriodicUpsertAggregator` 是通用骨架；:data:`session_activity_recorder`
与 :data:`command_stat_recorder` 是两个具体实例。

``record`` 同步、无 IO，在单事件循环内对 ``_pending`` 的读改写是原子的；``flush``
通过整体替换字典引用做快照，无需显式锁。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from astrbot.core import db_helper, logger


class PeriodicUpsertAggregator:
    def __init__(
        self,
        *,
        name: str,
        key_fields: tuple[str, ...],
        flush_fn: Callable[[list[dict]], Awaitable[None]],
        count_field: str = "count",
        refresh_fields: tuple[str, ...] = (),
        flush_interval_seconds: int = 60,
        max_pending: int = 2000,
    ) -> None:
        """
        Args:
            name: 用于日志的可读名称。
            key_fields: 组成去重键的字段名（同键记录会被合并）。
            flush_fn: 批量落库回调，接收合并后的记录列表。
            count_field: 累加计数的字段名。
            refresh_fields: 需要用最新非空值刷新的字段（如名称）。
            flush_interval_seconds: 周期 flush 间隔。
            max_pending: 内存待写上限，超过立即触发一次 flush。
        """
        self._name = name
        self._key_fields = key_fields
        self._flush_fn = flush_fn
        self._count_field = count_field
        self._refresh_fields = refresh_fields
        self._flush_interval_seconds = flush_interval_seconds
        self._max_pending = max_pending
        self._pending: dict[tuple, dict] = {}
        self._flush_task: asyncio.Task | None = None

    def record(self, **fields) -> None:
        """在内存中合并一条记录（同步、无 IO）。"""
        key = tuple(fields.get(field) for field in self._key_fields)
        entry = self._pending.get(key)
        if entry is None:
            entry = dict(fields)
            entry[self._count_field] = fields.get(self._count_field, 1)
            self._pending[key] = entry
        else:
            entry[self._count_field] = entry.get(self._count_field, 0) + fields.get(
                self._count_field, 1
            )
            for field in self._refresh_fields:
                value = fields.get(field)
                if value:
                    entry[field] = value

        self._ensure_flush_task()
        if len(self._pending) > self._max_pending:
            asyncio.create_task(self.flush())

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._flush_periodically())
        except RuntimeError:
            # 没有运行中的事件循环（例如同步测试场景），留待下次调用重试
            self._flush_task = None

    async def _flush_periodically(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval_seconds)
                await self.flush()
                if not self._pending:
                    self._flush_task = None
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"{self._name}后台刷写任务异常", exc_info=True)
        finally:
            if self._flush_task is asyncio.current_task():
                self._flush_task = None

    async def flush(self) -> None:
        """把当前内存快照批量写入数据库。"""
        if not self._pending:
            return
        snapshot = self._pending
        self._pending = {}  # 原子交换：单事件循环内安全
        records = list(snapshot.values())
        try:
            await self._flush_fn(records)
        except Exception as e:
            logger.error(f"批量写入{self._name}失败: {e}")
            self._merge_back(records)

    def _merge_back(self, records: list[dict]) -> None:
        """写库失败时把记录合并回内存，等待下次重试。"""
        for record in records:
            key = tuple(record.get(field) for field in self._key_fields)
            entry = self._pending.get(key)
            if entry is None:
                self._pending[key] = record
            else:
                entry[self._count_field] = entry.get(self._count_field, 0) + record.get(
                    self._count_field, 0
                )


# 会话活跃统计（独立用户 / 独立群），仅统计机器人实际处理并回复的消息。
session_activity_recorder = PeriodicUpsertAggregator(
    name="会话活跃统计",
    key_fields=("date", "platform_id", "group_id", "user_id"),
    flush_fn=lambda records: db_helper.insert_session_activity_stats_batch(records),
    refresh_fields=("platform_type", "group_name", "user_name"),
)

# 指令 / 正则处理器触发统计（按小时聚合）。
command_stat_recorder = PeriodicUpsertAggregator(
    name="指令使用统计",
    key_fields=(
        "timestamp",
        "platform_id",
        "trigger_type",
        "plugin_name",
        "command_name",
    ),
    flush_fn=lambda records: db_helper.insert_command_stats_batch(records),
)
