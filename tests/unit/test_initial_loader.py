"""Signal-driven shutdown of InitialLoader."""

import asyncio
import contextlib
import os
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core import initial_loader


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="os.kill(SIGTERM) terminates on Windows")
async def test_repeated_sigterm_runs_one_stop_and_restores_handlers(monkeypatch):
    started = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()

    async def run_core():
        started.set()
        await asyncio.Event().wait()

    async def stop():
        stopping.set()
        await release.wait()

    lifecycle = MagicMock()
    lifecycle.initialize = AsyncMock()
    lifecycle.start = run_core
    lifecycle.stop = AsyncMock(side_effect=stop)
    lifecycle.dashboard_shutdown_event = asyncio.Event()
    dashboard = MagicMock()
    dashboard.run = lifecycle.dashboard_shutdown_event.wait
    monkeypatch.setattr(initial_loader, "AstrBotCoreLifecycle", lambda *_: lifecycle)
    monkeypatch.setattr(initial_loader, "AstrBotDashboard", lambda *_: dashboard)

    leaked = []

    def guard(*args):
        leaked.append(args)

    # Without the loader's handler the signal lands here instead of killing pytest.
    previous = signal.signal(signal.SIGTERM, guard)
    sigint_before = signal.getsignal(signal.SIGINT)
    running = asyncio.create_task(
        initial_loader.InitialLoader(MagicMock(), MagicMock()).start()
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopping.wait(), 5)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.wait_for(running, 5)

        assert lifecycle.stop.await_count == 1
        assert lifecycle.dashboard_shutdown_event.is_set()
        assert leaked == []
        assert signal.getsignal(signal.SIGTERM) is guard
        assert signal.getsignal(signal.SIGINT) is sigint_before
    finally:
        release.set()
        running.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(running, 5)
        signal.signal(signal.SIGTERM, previous)
