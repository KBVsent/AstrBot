"""Reassign legacy platform_stats rows from adapter type names to adapter instances."""

from sqlalchemy import text

from astrbot.api import logger, sp
from astrbot.core.db import BaseDatabase


async def migrate_platform_stats_instances(
    db_helper: BaseDatabase, platform_configs: list[dict]
) -> None:
    """Move legacy send counts of each single-instance adapter type to that instance.

    Legacy rows carry the type name in platform_id and "unknown" or the type name in
    platform_type. Types with no instance or several instances keep their rows.

    Args:
        db_helper: Main database.
        platform_configs: Platform entries of the default configuration.
    """
    flag = "migration_done_platform_stats_instances_1"
    if await db_helper.get_preference("global", "global", flag):
        return

    # WebChat is always exactly one instance and is not listed in the configuration.
    instances: dict[str, list[str]] = {"webchat": ["webchat"]}
    for platform_config in platform_configs:
        platform_type = platform_config.get("type")
        platform_id = platform_config.get("id")
        if platform_type and platform_id:
            instances.setdefault(platform_type, []).append(platform_id)

    # The second clause picks up rows already written under the target key, so the
    # rewrite never collides with the unique constraint and a rerun changes nothing.
    source = (
        "(platform_id = :type AND platform_type IN ('unknown', :type))"
        " OR (platform_id = :id AND platform_type = :type)"
    )
    migrated_types = []
    async with db_helper.get_db() as session:
        async with session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            for platform_type, ids in instances.items():
                if len(ids) != 1:
                    continue
                params = {"type": platform_type, "id": ids[0]}
                # Raw timestamp text is kept as stored, so grouping matches the key.
                totals = (
                    await session.execute(
                        text(
                            "SELECT timestamp, SUM(count) FROM platform_stats"
                            f" WHERE {source} GROUP BY timestamp"
                        ),
                        params,
                    )
                ).all()
                if not totals:
                    continue
                await session.execute(
                    text(f"DELETE FROM platform_stats WHERE {source}"), params
                )
                await session.execute(
                    text(
                        "INSERT INTO platform_stats"
                        " (timestamp, platform_id, platform_type, count)"
                        " VALUES (:timestamp, :id, :type, :count)"
                    ),
                    [
                        {**params, "timestamp": timestamp, "count": count}
                        for timestamp, count in totals
                    ],
                )
                migrated_types.append(platform_type)

    await sp.put_async("global", "global", flag, True)
    if migrated_types:
        logger.info(
            "platform_stats 历史发送计数已归到适配器实例: %s", ", ".join(migrated_types)
        )
