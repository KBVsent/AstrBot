import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import astrbot.dashboard.services.stat_service as stat_service
from astrbot.core.astr_agent_runtime.computer import process_sandbox
from astrbot.core.astr_agent_runtime.computer.process_sandbox import (
    SandboxRunResult,
    SandboxTimeoutError,
)
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
    "daily_usage",
    "system",
}


@pytest.fixture(autouse=True)
def reset_sandbox_probe():
    """Keep cached sandbox capability checks independent across tests."""
    process_sandbox.get_process_sandbox_status.cache_clear()
    yield
    process_sandbox.get_process_sandbox_status.cache_clear()


def _make_service(db) -> StatService:
    """Build a StatService with a real DB and a mocked core lifecycle."""
    core_lifecycle = MagicMock()
    core_lifecycle.star_context.get_all_stars.return_value = []
    core_lifecycle.platform_manager.get_insts.return_value = []
    core_lifecycle.start_time = int(time.time()) - 100
    return StatService(db_helper=db, core_lifecycle=core_lifecycle, config={})


def _day_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize(
    ("system", "arch", "executable", "backend", "status"),
    [
        ("Linux", "x86_64", "/usr/bin/bwrap", "bubblewrap", "detected"),
        ("Linux", "aarch64", None, "bubblewrap", "missing"),
        ("Darwin", "arm64", "/usr/bin/sandbox-exec", "seatbelt", "detected"),
        ("Darwin", "x86_64", None, "seatbelt", "missing"),
        ("Darwin", "arm64", "/opt/bin/sandbox-exec", "seatbelt", "missing"),
        ("Windows", "AMD64", None, "appcontainer", "detected"),
        ("Windows", "ARM64", None, "appcontainer", "detected"),
        ("Windows", "AMD64", "/usr/bin/bwrap", "appcontainer", "detected"),
        ("FreeBSD", "", None, None, "unsupported"),
    ],
)
def test_runtime_detects_platform_dependencies(
    monkeypatch, tmp_path, system, arch, executable, backend, status
):
    """Only report a detected sandbox after its launch probe succeeds."""
    monkeypatch.setattr(
        stat_service,
        "platform",
        SimpleNamespace(system=lambda: system, machine=lambda: arch),
    )
    monkeypatch.setattr(process_sandbox, "platform", stat_service.platform)
    which = Mock(return_value=executable)
    monkeypatch.setattr(process_sandbox, "shutil", SimpleNamespace(which=which))
    sandbox = Mock()
    sandbox.run.return_value = SandboxRunResult(returncode=0)
    factory = Mock(return_value=sandbox)
    monkeypatch.setattr(process_sandbox, "create_process_sandbox", factory)
    monkeypatch.setattr(process_sandbox, "get_astrbot_temp_path", lambda: str(tmp_path))

    service = _make_service(MagicMock())

    assert service.runtime == {
        "os": system.lower(),
        "arch": arch,
        "sandbox": {"backend": backend, "status": status},
    }
    assert process_sandbox.get_process_sandbox_status() == service.runtime["sandbox"]
    if system == "Linux":
        which.assert_called_once_with("bwrap")
    elif system == "Darwin":
        which.assert_called_once_with("sandbox-exec", path="/usr/bin")
    else:
        which.assert_not_called()
    if status == "detected":
        factory.assert_called_once_with()
        sandbox.run.assert_called_once()
        args, kwargs = sandbox.run.call_args
        assert args[0] == (
            [sys.executable, "-I", "-c", "pass"]
            if system == "Windows"
            else ["/bin/sh", "-c", ":"]
        )
        assert args[1].filesystem_scope == "workspace"
        assert args[1].allow_network is False
        assert not args[1].workspace.exists()
        assert kwargs["timeout"] == 5
    else:
        factory.assert_not_called()


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (
            SandboxRunResult(
                returncode=1,
                stderr=b"bwrap: Creating new namespace failed: Operation not permitted\n",
            ),
            "bwrap: Creating new namespace failed: Operation not permitted",
        ),
        (
            SandboxRunResult(
                returncode=1, stderr=b"bwrap: setting up uid map: Permission denied\n"
            ),
            "bwrap: setting up uid map: Permission denied",
        ),
        (
            SandboxRunResult(
                returncode=1,
                stderr=b"bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n",
            ),
            "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
        ),
        (SandboxRunResult(returncode=2), "Sandbox probe exited with code 2."),
        (PermissionError("Operation not permitted"), "Operation not permitted"),
        (SandboxTimeoutError("Sandbox probe timed out."), "Sandbox probe timed out."),
        (SandboxTimeoutError(), "SandboxTimeoutError"),
        (TimeoutError("Sandbox probe timed out."), "Sandbox probe timed out."),
        (TimeoutError(), "TimeoutError"),
        (RuntimeError("Sandbox launch failed."), "Sandbox launch failed."),
    ],
)
def test_runtime_reports_sandbox_startup_failure(
    monkeypatch, tmp_path, system, outcome, error
):
    """Distinguish installed but unusable sandboxes without preventing startup."""
    monkeypatch.setattr(stat_service.platform, "system", lambda: system)
    monkeypatch.setattr(
        process_sandbox,
        "shutil",
        SimpleNamespace(which=lambda name, **kwargs: f"/usr/bin/{name}"),
    )
    sandbox = Mock()
    if isinstance(outcome, Exception):
        sandbox.run.side_effect = outcome
    else:
        sandbox.run.return_value = outcome
    monkeypatch.setattr(process_sandbox, "create_process_sandbox", lambda: sandbox)
    monkeypatch.setattr(process_sandbox, "get_astrbot_temp_path", lambda: str(tmp_path))

    service = _make_service(MagicMock())

    assert service.runtime["sandbox"] == {
        "backend": {
            "Linux": "bubblewrap",
            "Darwin": "seatbelt",
            "Windows": "appcontainer",
        }[system],
        "status": "unavailable",
        "error": error,
    }
    assert process_sandbox.get_process_sandbox_status() == service.runtime["sandbox"]
    sandbox.run.assert_called_once()
    assert not list(tmp_path.iterdir())


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


@pytest.mark.asyncio
async def test_provider_token_ranking_includes_umo_display_names(temp_db):
    """UMO token rankings should prefer aliases and fall back to raw identifiers."""
    aliased_umo = "qq:GroupMessage:group-1"
    raw_umo = "webchat:FriendMessage:session-2"
    await temp_db.insert_provider_stat(
        umo=aliased_umo,
        provider_id="provider-1",
        stats={"token_usage": {"input_other": 3, "input_cached": 4, "output": 5}},
    )
    await temp_db.insert_provider_stat(
        umo=raw_umo,
        provider_id="provider-1",
        stats={"token_usage": {"input_other": 1, "input_cached": 1, "output": 1}},
    )
    await temp_db.upsert_umo_alias(
        umo=aliased_umo,
        creator_sender_id="creator-1",
        auto_name="研发群",
        user_alias="产品讨论群",
    )

    service = _make_service(temp_db)
    service.config = {
        "platform": [{"id": "qq", "type": "qq_official"}],
    }
    result = await service.get_provider_token_stats()

    assert result["range_by_umo"] == [
        {
            "umo": aliased_umo,
            "display_name": "产品讨论群",
            "platform_type": "qq_official",
            "tokens": 12,
        },
        {
            "umo": raw_umo,
            "display_name": raw_umo,
            "platform_type": "webchat",
            "tokens": 3,
        },
    ]
