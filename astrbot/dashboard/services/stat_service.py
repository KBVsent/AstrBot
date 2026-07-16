from __future__ import annotations

import ast
import asyncio
import re
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from pathlib import Path

import aiohttp
import psutil
from sqlmodel import col, select

from astrbot.core import DEMO_MODE, logger
from astrbot.core.config import VERSION
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.core.db.po import ProviderStat
from astrbot.core.desktop_runtime import (
    DESKTOP_MANAGED_RESTART_MESSAGE,
    is_desktop_managed_backend,
)
from astrbot.core.utils.astrbot_path import get_astrbot_path
from astrbot.core.utils.auth_password import (
    is_default_dashboard_password,
    is_md5_dashboard_password,
)
from astrbot.core.utils.io import get_dashboard_dist_version, get_dashboard_version
from astrbot.core.utils.storage_cleaner import StorageCleaner
from astrbot.core.utils.version_comparator import VersionComparator
from astrbot.dashboard.password_state import (
    get_dashboard_password_hash,
    is_password_change_required,
    is_password_storage_upgraded,
)


class StatServiceError(Exception):
    pass


class StatService:
    def __init__(
        self,
        db_helper: BaseDatabase,
        core_lifecycle: AstrBotCoreLifecycle,
        config: AstrBotConfig,
    ) -> None:
        self.db_helper = db_helper
        self.core_lifecycle = core_lifecycle
        self.config = config
        self.storage_cleaner = StorageCleaner(config)

    async def restart_core(self) -> None:
        if DEMO_MODE:
            raise StatServiceError(
                "You are not permitted to do this operation in demo mode"
            )
        if is_desktop_managed_backend():
            raise StatServiceError(DESKTOP_MANAGED_RESTART_MESSAGE)

        await self.core_lifecycle.restart()

    @staticmethod
    def get_running_time_components(total_seconds: int):
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return {"hours": hours, "minutes": minutes, "seconds": seconds}

    async def is_default_cred(self):
        password_change_required = await is_password_change_required(
            self.db_helper,
            self.config,
        )
        if password_change_required:
            return not DEMO_MODE

        storage_upgraded = await is_password_storage_upgraded(
            self.db_helper,
            self.config,
        )
        if not storage_upgraded:
            return False

        username = self.config["dashboard"]["username"]
        password = get_dashboard_password_hash(self.config, upgraded=True)
        return (
            username == "astrbot" and is_default_dashboard_password(password)
        ) and not DEMO_MODE

    async def get_version(self) -> dict:
        storage_upgraded = await is_password_storage_upgraded(
            self.db_helper,
            self.config,
        )
        password = get_dashboard_password_hash(
            self.config,
            upgraded=storage_upgraded,
        )

        md5_pwd_hint = is_md5_dashboard_password(password)
        return {
            "version": VERSION,
            "dashboard_version": await get_dashboard_version(),
            "change_pwd_hint": await self.is_default_cred(),
            "md5_pwd_hint": md5_pwd_hint,
            "password_upgrade_required": not storage_upgraded,
        }

    async def get_public_versions(
        self,
        dashboard_static_folder: str | None = None,
    ) -> dict:
        """Return version details that are safe to expose before login.

        Args:
            dashboard_static_folder: Static WebUI dist directory currently served by
                the dashboard, when available.

        Returns:
            Public WebUI and AstrBot version information.
        """

        def read_code_version() -> str | None:
            """Read the AstrBot code version from the package file.

            Returns:
                The version string from disk, or None when it is unavailable.
            """

            version_file = Path(get_astrbot_path()) / "astrbot" / "__init__.py"
            module = ast.parse(version_file.read_text(encoding="utf-8"))
            for statement in module.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "__version__"
                    for target in statement.targets
                ):
                    continue
                if isinstance(statement.value, ast.Constant) and isinstance(
                    statement.value.value,
                    str,
                ):
                    return statement.value.value.strip()
                return None
            return None

        dashboard_version = None
        try:
            if dashboard_static_folder:
                dashboard_version = get_dashboard_dist_version(
                    Path(dashboard_static_folder)
                )
            if dashboard_version is None:
                dashboard_version = await get_dashboard_version()
        except Exception as exc:
            logger.warning("Failed to read public WebUI version: %s", exc)

        code_version = None
        try:
            code_version = await asyncio.to_thread(read_code_version)
        except Exception as exc:
            logger.warning("Failed to read AstrBot code version from disk: %s", exc)

        return {
            "webui_version": dashboard_version,
            "astrbot_version": VERSION,
            "astrbot_code_version": code_version,
        }

    def get_start_time(self) -> dict:
        return {"start_time": self.core_lifecycle.start_time}

    async def get_storage_status(self) -> dict:
        try:
            return await asyncio.to_thread(self.storage_cleaner.get_status)
        except Exception as exc:
            logger.error("获取存储占用失败", exc_info=True)
            raise StatServiceError(
                "获取存储占用失败，请查看后端日志了解详情。"
            ) from exc

    async def cleanup_storage(self, target: str) -> dict:
        try:
            return await asyncio.to_thread(self.storage_cleaner.cleanup, target)
        except ValueError as exc:
            raise StatServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("清理存储失败", exc_info=True)
            raise StatServiceError("清理存储失败，请查看后端日志了解详情。") from exc

    def _resolve_day(
        self, date_str: str | None
    ) -> tuple[str, datetime, datetime, datetime, datetime]:
        """把 "YYYY-MM-DD"（默认今天）解析为本地自然日的边界。

        Returns:
            (归一化日期串, naive 本地起, naive 本地止, aware 本地起, aware 本地止)。
            naive 版用于按 naive 本地时间存储的 platform_stats / command_stats；
            aware 版用于换算成 UTC 查询 provider_stats.created_at。
        """
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError as exc:
                raise StatServiceError("date must be in YYYY-MM-DD format") from exc
        else:
            day = datetime.now(local_tz).replace(tzinfo=None)
        naive_start = datetime(day.year, day.month, day.day)
        naive_end = naive_start + timedelta(days=1)
        aware_start = naive_start.replace(tzinfo=local_tz)
        aware_end = naive_end.replace(tzinfo=local_tz)
        return (
            naive_start.strftime("%Y-%m-%d"),
            naive_start,
            naive_end,
            aware_start,
            aware_end,
        )

    def _resolve_platform_keys(
        self, platform_id: str | None
    ) -> tuple[str | None, str | None]:
        """把下拉选中的适配器实例 id 解析为各统计表使用的过滤键。

        Returns:
            (id_key, name_key)。id_key 用于 command_stats / session_activity_stats /
            provider_stats(umo 前缀)；name_key（适配器类型名）用于 platform_stats。
            单实例默认部署下二者相同。
        """
        if not platform_id:
            return None, None
        name_key = None
        for inst in self.core_lifecycle.platform_manager.get_insts():
            try:
                meta = inst.meta()
            except Exception:
                continue
            if meta.id == platform_id:
                name_key = meta.name
                break
        return platform_id, (name_key or platform_id)

    def _list_available_platforms(self) -> list[dict]:
        """列出当前已配置的适配器实例，供前端下拉筛选使用。"""
        platforms = []
        for inst in self.core_lifecycle.platform_manager.get_insts():
            try:
                meta = inst.meta()
            except Exception:
                continue
            platforms.append(
                {
                    "id": meta.id,
                    "name": meta.adapter_display_name or meta.name,
                    "type": meta.name,
                }
            )
        return platforms

    async def get_stat(
        self, date: str | None = None, platform_id: str | None = None
    ) -> dict:
        try:
            date_norm, naive_start, naive_end, _, _ = self._resolve_day(date)
            _, name_key = self._resolve_platform_keys(platform_id)

            rows = await self.db_helper.get_platform_stats_in_range(
                naive_start, naive_end, name_key
            )
            hourly: dict[int, int] = defaultdict(int)
            for row in rows:
                bucket = row.timestamp.replace(minute=0, second=0, microsecond=0)
                hourly[int(bucket.timestamp())] += row.count
            message_time_series = [
                [
                    int((naive_start + timedelta(hours=hour)).timestamp()),
                    hourly.get(
                        int((naive_start + timedelta(hours=hour)).timestamp()), 0
                    ),
                ]
                for hour in range(24)
            ]

            grouped = await self.db_helper.get_grouped_platform_stats_in_range(
                naive_start, naive_end, name_key
            )
            platform = [{"name": pid, "count": count} for pid, count in grouped]
            message_count = await self.db_helper.get_total_message_count_in_range(
                naive_start, naive_end, name_key
            )

            cpu_percent = psutil.cpu_percent(interval=0.5)
            thread_count = threading.active_count()

            plugins = self.core_lifecycle.star_context.get_all_stars()
            plugin_info = [
                {
                    "name": getattr(plugin, "name", plugin.__class__.__name__),
                    "version": getattr(plugin, "version", "1.0.0"),
                    "is_enabled": True,
                }
                for plugin in plugins
            ]

            running_time = self.get_running_time_components(
                int(time.time()) - self.core_lifecycle.start_time,
            )

            return {
                "date": date_norm,
                "platform_id": platform_id or "",
                "available_platforms": self._list_available_platforms(),
                "platform": platform,
                "message_count": message_count,
                "platform_count": len(
                    self.core_lifecycle.platform_manager.get_insts(),
                ),
                "plugin_count": len(plugins),
                "plugins": plugin_info,
                "message_time_series": message_time_series,
                "running": running_time,
                "memory": {
                    "process": psutil.Process().memory_info().rss >> 20,
                    "system": psutil.virtual_memory().total >> 20,
                },
                "cpu_percent": round(cpu_percent, 1),
                "thread_count": thread_count,
                "start_time": self.core_lifecycle.start_time,
            }
        except StatServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(str(exc)) from exc

    async def get_top_commands(
        self,
        date: str | None = None,
        platform_id: str | None = None,
        limit: int = 20,
    ) -> dict:
        """获取指定自然日内最常被触发的指令排行。"""
        try:
            _, naive_start, naive_end, _, _ = self._resolve_day(date)
            id_key, _ = self._resolve_platform_keys(platform_id)
            rows = await self.db_helper.get_top_commands(
                naive_start, naive_end, id_key, limit
            )
        except StatServiceError:
            raise
        except Exception as e:
            logger.error(traceback.format_exc())
            raise StatServiceError(str(e)) from e
        commands = [
            {
                "command_name": command_name,
                "plugin_name": plugin_name,
                "trigger_type": trigger_type,
                "count": count,
            }
            for command_name, plugin_name, trigger_type, count in rows
        ]
        return {"commands": commands}

    async def get_active_session_stats(
        self,
        date: str | None = None,
        platform_id: str | None = None,
        limit: int = 20,
    ) -> dict:
        """获取指定自然日的独立用户数 / 独立群数及活跃排行。"""
        try:
            date_norm, *_ = self._resolve_day(date)
            id_key, _ = self._resolve_platform_keys(platform_id)
            data = await self.db_helper.get_active_session_stats(
                date_norm, id_key, limit
            )
        except StatServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(str(exc)) from exc
        data["date"] = date_norm
        data["platform_id"] = platform_id or ""
        return data

    @staticmethod
    def _ensure_aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def get_provider_token_stats(
        self, date: str | None = None, platform_id: str | None = None
    ) -> dict:
        try:
            _, _, _, aware_start, aware_end = self._resolve_day(date)
            id_key, _ = self._resolve_platform_keys(platform_id)
            local_tz = aware_start.tzinfo or timezone.utc
            query_start_utc = aware_start.astimezone(timezone.utc)
            query_end_utc = aware_end.astimezone(timezone.utc)

            async with self.db_helper.get_db() as session:
                stmt = select(ProviderStat).where(
                    ProviderStat.agent_type == "internal",
                    ProviderStat.created_at >= query_start_utc,
                    ProviderStat.created_at < query_end_utc,
                )
                if id_key:
                    # umo 形如 "platform_id:message_type:session_id"，前缀即平台实例 id
                    stmt = stmt.where(col(ProviderStat.umo).like(f"{id_key}:%"))
                stmt = stmt.order_by(col(ProviderStat.created_at).asc())
                result = await session.execute(stmt)
                records = result.scalars().all()

            bucket_timestamps: list[int] = [
                int((aware_start + timedelta(hours=hour)).timestamp() * 1000)
                for hour in range(24)
            ]

            trend_by_provider: dict[str, dict[int, int]] = defaultdict(
                lambda: defaultdict(int)
            )
            total_by_provider: dict[str, int] = defaultdict(int)
            total_by_umo: dict[str, int] = defaultdict(int)
            total_by_bucket: dict[int, int] = defaultdict(int)
            by_model: dict[str, int] = defaultdict(int)
            range_total_tokens = 0
            range_total_output_tokens = 0
            range_total_calls = 0
            range_success_calls = 0
            range_ttft_total_ms = 0.0
            range_ttft_samples = 0
            range_duration_total_ms = 0.0
            range_duration_samples = 0

            for record in records:
                created_at_utc = self._ensure_aware_utc(record.created_at)
                created_at_local = created_at_utc.astimezone(local_tz)
                token_total = (
                    record.token_input_other
                    + record.token_input_cached
                    + record.token_output
                )
                provider_id = record.provider_id or "unknown"
                provider_model = record.provider_model or "Unknown"

                bucket_local = created_at_local.replace(
                    minute=0, second=0, microsecond=0
                )
                bucket_ts = int(bucket_local.timestamp() * 1000)
                trend_by_provider[provider_id][bucket_ts] += token_total
                total_by_provider[provider_id] += token_total
                total_by_umo[record.umo or "unknown"] += token_total
                total_by_bucket[bucket_ts] += token_total
                by_model[provider_model] += token_total
                range_total_tokens += token_total
                range_total_calls += 1
                if record.status != "error":
                    range_success_calls += 1
                if record.time_to_first_token > 0:
                    range_ttft_total_ms += record.time_to_first_token * 1000
                    range_ttft_samples += 1
                if record.end_time > record.start_time:
                    range_duration_total_ms += (
                        record.end_time - record.start_time
                    ) * 1000
                    range_duration_samples += 1
                    range_total_output_tokens += record.token_output

            # 单自然日视图下 "当日" 与 "区间" 一致，today_* 字段沿用以兼容前端
            today_by_model = by_model
            today_by_provider = total_by_provider
            today_total_tokens = range_total_tokens
            today_total_calls = range_total_calls

            sorted_provider_ids = sorted(
                total_by_provider.keys(),
                key=lambda item: total_by_provider[item],
                reverse=True,
            )

            series = [
                {
                    "name": provider_id,
                    "data": [
                        [bucket_ts, trend_by_provider[provider_id].get(bucket_ts, 0)]
                        for bucket_ts in bucket_timestamps
                    ],
                    "total_tokens": total_by_provider[provider_id],
                }
                for provider_id in sorted_provider_ids
            ]

            total_series = [
                [bucket_ts, total_by_bucket.get(bucket_ts, 0)]
                for bucket_ts in bucket_timestamps
            ]

            today_by_model_data = [
                {"provider_model": model_name, "tokens": tokens}
                for model_name, tokens in sorted(
                    today_by_model.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]
            today_by_provider_data = [
                {"provider_id": provider_id, "tokens": tokens}
                for provider_id, tokens in sorted(
                    today_by_provider.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]
            range_by_provider_data = [
                {"provider_id": provider_id, "tokens": tokens}
                for provider_id, tokens in sorted(
                    total_by_provider.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]
            range_by_umo_data = [
                {"umo": umo, "tokens": tokens}
                for umo, tokens in sorted(
                    total_by_umo.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]

            return {
                "date": aware_start.strftime("%Y-%m-%d"),
                "platform_id": platform_id or "",
                "trend": {
                    "series": series,
                    "total_series": total_series,
                },
                "range_total_tokens": range_total_tokens,
                "range_total_calls": range_total_calls,
                "range_avg_ttft_ms": (
                    range_ttft_total_ms / range_ttft_samples
                    if range_ttft_samples
                    else 0
                ),
                "range_avg_duration_ms": (
                    range_duration_total_ms / range_duration_samples
                    if range_duration_samples
                    else 0
                ),
                "range_avg_tpm": (
                    range_total_output_tokens / (range_duration_total_ms / 1000 / 60)
                    if range_duration_total_ms > 0
                    else 0
                ),
                "range_success_rate": (
                    range_success_calls / range_total_calls if range_total_calls else 0
                ),
                "range_by_provider": range_by_provider_data,
                "range_by_umo": range_by_umo_data,
                "today_total_tokens": today_total_tokens,
                "today_total_calls": today_total_calls,
                "today_by_model": today_by_model_data,
                "today_by_provider": today_by_provider_data,
            }
        except StatServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(f"Error: {exc!s}") from exc

    async def test_ghproxy_connection(self, proxy_url: str | None) -> dict:
        try:
            if not proxy_url:
                raise StatServiceError("proxy_url is required")

            proxy_url = proxy_url.rstrip("/")
            test_url = f"{proxy_url}/https://github.com/AstrBotDevs/AstrBot/raw/refs/heads/master/.python-version"
            start_time = time.time()

            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    test_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response,
            ):
                if response.status == 200:
                    end_time = time.time()
                    _ = await response.text()
                    return {
                        "latency": round((end_time - start_time) * 1000, 2),
                    }
                raise StatServiceError(f"Failed. Status code: {response.status}")
        except StatServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(f"Error: {exc!s}") from exc

    def get_changelog(self, version: str | None) -> dict:
        try:
            if not version:
                raise StatServiceError("version parameter is required")

            version = version.lstrip("v")
            if not re.match(r"^[a-zA-Z0-9._-]+$", version):
                raise StatServiceError("Invalid version format")
            if ".." in version or "/" in version or "\\" in version:
                raise StatServiceError("Invalid version format")

            changelogs_dir = (Path(get_astrbot_path()) / "changelogs").resolve()
            changelog_path = (changelogs_dir / f"v{version}.md").resolve(strict=False)
            if not changelog_path.is_relative_to(changelogs_dir):
                logger.warning(
                    "Path traversal attempt detected: %s -> %s",
                    version,
                    changelog_path,
                )
                raise StatServiceError("Invalid version format")

            if not changelog_path.is_file():
                raise StatServiceError(f"Changelog for version {version} not found")

            content = changelog_path.read_text(encoding="utf-8")
            return {"content": content, "version": version}
        except StatServiceError:
            raise
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(f"Error: {exc!s}") from exc

    def list_changelog_versions(self) -> dict:
        try:
            changelogs_dir = Path(get_astrbot_path()) / "changelogs"
            if not changelogs_dir.exists():
                return {"versions": []}

            versions = []
            for path in changelogs_dir.iterdir():
                filename = path.name
                if filename.endswith(".md") and filename.startswith("v"):
                    version = filename[1:-3]
                    if re.match(r"^[a-zA-Z0-9._-]+$", version):
                        versions.append(version)

            versions.sort(
                key=cmp_to_key(
                    lambda v1, v2: VersionComparator.compare_version(v2, v1),
                ),
            )

            return {"versions": versions}
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(f"Error: {exc!s}") from exc

    def get_first_notice(self, locale: str | None) -> dict:
        try:
            locale = (locale or "").strip()
            if not re.match(r"^[A-Za-z0-9_-]*$", locale):
                locale = ""

            base_path = Path(get_astrbot_path())
            candidates: list[Path] = []

            if locale:
                candidates.append(base_path / f"FIRST_NOTICE.{locale}.md")
                if locale.lower().startswith("zh"):
                    candidates.append(base_path / "FIRST_NOTICE.md")
                    candidates.append(base_path / "FIRST_NOTICE.zh-CN.md")
                elif locale.lower().startswith("en"):
                    candidates.append(base_path / "FIRST_NOTICE.en-US.md")

            candidates.extend(
                [
                    base_path / "FIRST_NOTICE.md",
                    base_path / "FIRST_NOTICE.en-US.md",
                ],
            )

            for notice_path in candidates:
                if not notice_path.is_file():
                    continue
                content = notice_path.read_text(encoding="utf-8")
                if content.strip():
                    return {"content": content}

            return {"content": None}
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise StatServiceError(f"Error: {exc!s}") from exc
