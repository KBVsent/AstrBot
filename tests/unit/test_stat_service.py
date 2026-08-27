import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from astrbot.dashboard.services.stat_service import StatService, StatServiceError

RESPONSE_KEYS = {
    "date",
    "platform_id",
    "available_platforms",
    "platform",
    "message_count",
    "previous_message_count",
    "platform_count",
    "plugin_count",
    "plugins",
    "message_time_series",
    "running",
    "memory",
    "cpu_percent",
    "thread_count",
    "start_time",
}


def _make_service(db) -> StatService:
    """Build a StatService with a real DB and a mocked core lifecycle."""
    core_lifecycle = MagicMock()
    core_lifecycle.star_context.get_all_stars.return_value = []
    core_lifecycle.platform_manager.get_insts.return_value = []
    core_lifecycle.start_time = int(time.time()) - 100
    return StatService(db_helper=db, core_lifecycle=core_lifecycle, config={})


def _day_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_get_stat_aggregates_platform_stats_for_the_natural_day(temp_db):
    """当天的行按平台聚合并落进 24 个小时桶，前一天的行只计入环比。"""
    day_start = _day_start()
    seed = [
        ("aiocqhttp", 3, day_start + timedelta(hours=2)),
        ("aiocqhttp", 5, day_start + timedelta(hours=2, minutes=30)),
        ("qqofficial", 2, day_start + timedelta(hours=5)),
        ("webchat", 7, day_start + timedelta(hours=23)),
        # 前一天：只出现在 previous_message_count 里。
        ("aiocqhttp", 4, day_start - timedelta(hours=2)),
    ]
    for platform_id, count, ts in seed:
        await temp_db.insert_platform_stats(platform_id, platform_id, count, ts)

    result = await _make_service(temp_db).get_stat()

    assert result["date"] == day_start.strftime("%Y-%m-%d")
    assert result["platform_id"] == ""
    assert result["message_count"] == 17
    assert result["previous_message_count"] == 4

    platform = {entry["name"]: entry["count"] for entry in result["platform"]}
    assert platform == {"aiocqhttp": 8, "qqofficial": 2, "webchat": 7}
    for entry in result["platform"]:
        assert set(entry) == {"name", "count"}

    series = result["message_time_series"]
    assert len(series) == 24
    bucket_starts = [bucket_start for bucket_start, _ in series]
    assert bucket_starts == sorted(bucket_starts)
    assert bucket_starts[0] == int(day_start.timestamp())
    assert sum(count for _, count in series) == 17
    by_hour = dict(series)
    assert by_hour[int((day_start + timedelta(hours=2)).timestamp())] == 8
    assert by_hour[int((day_start + timedelta(hours=5)).timestamp())] == 2
    assert by_hour[int((day_start + timedelta(hours=23)).timestamp())] == 7

    assert set(result) == RESPONSE_KEYS


@pytest.mark.asyncio
async def test_get_stat_empty_day(temp_db):
    """指定一个没有数据的自然日时，平台聚合为空且小时桶全零。"""
    day_start = _day_start()
    await temp_db.insert_platform_stats(
        "aiocqhttp", "aiocqhttp", 4, day_start + timedelta(hours=1)
    )

    target = (day_start - timedelta(days=3)).strftime("%Y-%m-%d")
    result = await _make_service(temp_db).get_stat(date=target)

    assert result["date"] == target
    assert result["platform"] == []
    assert result["message_count"] == 0
    assert result["previous_message_count"] == 0
    assert all(count == 0 for _, count in result["message_time_series"])


@pytest.mark.asyncio
async def test_get_stat_rejects_malformed_date(temp_db):
    """日期串格式非法时应抛出 StatServiceError 而不是被吞成通用错误。"""
    with pytest.raises(StatServiceError, match="YYYY-MM-DD"):
        await _make_service(temp_db).get_stat(date="2026/08/27")
