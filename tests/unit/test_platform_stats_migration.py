"""Legacy platform_stats rows move to the adapter instance that owns them."""

from datetime import datetime

import pytest
from sqlalchemy import text

from astrbot.core.db.migration import migra_platform_stats_instances as migration
from astrbot.core.utils.shared_preferences import SharedPreferences


@pytest.mark.asyncio
async def test_legacy_rows_merge_into_single_instance_and_rerun_is_stable(
    temp_db, monkeypatch
):
    await temp_db.initialize()
    prefs = SharedPreferences(temp_db)
    await prefs.initialize()
    monkeypatch.setattr(migration, "sp", prefs)
    hour = datetime(2026, 11, 1, 10)
    for platform_id, platform_type, count in [
        ("aiocqhttp", "unknown", 3),
        ("aiocqhttp", "aiocqhttp", 4),
        ("qq-main", "aiocqhttp", 5),
        ("telegram", "unknown", 2),
    ]:
        await temp_db.insert_platform_stats(platform_id, platform_type, count, hour)
    configs = [
        {"id": "qq-main", "type": "aiocqhttp", "enable": True},
        {"id": "tg-a", "type": "telegram", "enable": True},
        {"id": "tg-b", "type": "telegram", "enable": False},
    ]

    async def rows():
        async with temp_db.get_db() as session:
            result = await session.execute(
                text("SELECT platform_id, platform_type, count FROM platform_stats")
            )
            return sorted(tuple(row) for row in result.all())

    expected = [("qq-main", "aiocqhttp", 12), ("telegram", "unknown", 2)]
    await migration.migrate_platform_stats_instances(temp_db, configs)
    assert await rows() == expected

    await prefs.remove_async(
        "global", "global", "migration_done_platform_stats_instances_1"
    )
    await migration.migrate_platform_stats_instances(temp_db, configs)
    assert await rows() == expected
    await prefs.close()
