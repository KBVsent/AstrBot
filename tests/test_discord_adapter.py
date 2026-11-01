import asyncio
import base64
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from astrbot.api.message_components import Image, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.discord import (
    discord_platform_adapter,
    discord_platform_event,
)
from astrbot.core.platform.sources.discord.discord_platform_adapter import (
    DiscordPlatformAdapter,
)
from astrbot.core.platform.sources.discord.discord_platform_event import (
    DiscordPlatformEvent,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
_WAV_PATH = "/tmp/discord_voice.wav"


@pytest.mark.asyncio
async def test_discord_audio_attachment_resolves_to_wav_record(monkeypatch):
    class FakeMediaResolver:
        def __init__(self, media_ref: str, **kwargs) -> None:
            assert media_ref == "https://cdn.example/voice.ogg"
            assert kwargs["media_type"] == "audio"

        async def to_path(self, **kwargs) -> str:
            assert kwargs["target_format"] == "wav"
            return _WAV_PATH

    monkeypatch.setattr(
        discord_platform_adapter,
        "MediaResolver",
        FakeMediaResolver,
    )

    adapter = DiscordPlatformAdapter.__new__(DiscordPlatformAdapter)
    adapter.bot_self_id = "1"
    adapter.client = cast(Any, SimpleNamespace(user=SimpleNamespace(id=1)))

    message = SimpleNamespace(
        id=42,
        content="",
        channel=SimpleNamespace(id=123, guild=None),
        author=SimpleNamespace(id=2, display_name="tester"),
        attachments=[
            SimpleNamespace(
                content_type="audio/ogg",
                filename="voice.ogg",
                url="https://cdn.example/voice.ogg",
            )
        ],
        guild=None,
        role_mentions=[],
    )

    abm = await adapter.convert_message({"message": message})

    assert len(abm.message) == 1
    assert isinstance(abm.message[0], Record)
    assert abm.message[0].file == _WAV_PATH
    assert abm.message[0].url == _WAV_PATH
    assert abm.message[0].path == _WAV_PATH


@pytest.mark.asyncio
async def test_discord_send_image_resolves_data_uri_with_media_resolver(monkeypatch):
    captured = {}

    class FakeDiscordFile:
        def __init__(self, fp: BytesIO, filename: str) -> None:
            captured["bytes"] = fp.read()
            captured["filename"] = filename

    monkeypatch.setattr(discord_platform_event.discord, "File", FakeDiscordFile)

    event = DiscordPlatformEvent.__new__(DiscordPlatformEvent)
    image_base64 = base64.b64encode(_PNG_BYTES).decode("ascii")

    content, files, view, embeds, reference_message_id = await event._parse_to_discord(
        MessageChain(
            chain=[
                Image(file=f"data:image/png;base64,{image_base64}"),
            ]
        )
    )

    assert content == ""
    assert len(files) == 1
    assert captured["bytes"] == _PNG_BYTES
    assert captured["filename"] == "image.png"
    assert view is None
    assert embeds == []
    assert reference_message_id is None


@pytest.mark.asyncio
async def test_discord_send_record_resolves_audio_with_media_resolver(monkeypatch):
    captured = {}

    class FakeDiscordFile:
        def __init__(self, fp: BytesIO, filename: str) -> None:
            captured["bytes"] = fp.read()
            captured["filename"] = filename

    monkeypatch.setattr(discord_platform_event.discord, "File", FakeDiscordFile)

    event = DiscordPlatformEvent.__new__(DiscordPlatformEvent)
    audio_base64 = base64.b64encode(_WAV_BYTES).decode("ascii")

    content, files, view, embeds, reference_message_id = await event._parse_to_discord(
        MessageChain(
            chain=[
                Record.fromBase64(audio_base64),
            ]
        )
    )

    assert content == ""
    assert len(files) == 1
    assert captured["bytes"] == _WAV_BYTES
    assert captured["filename"] == "audio.wav"
    assert view is None
    assert embeds == []
    assert reference_message_id is None


def _slash_adapter() -> tuple[DiscordPlatformAdapter, AsyncMock]:
    adapter = DiscordPlatformAdapter.__new__(DiscordPlatformAdapter)
    adapter.bot_self_id = "1"
    handle_msg = AsyncMock()
    setattr(adapter, "handle_msg", handle_msg)
    return adapter, handle_msg


def _slash_ctx(defer, created_at) -> Any:
    return SimpleNamespace(
        defer=defer,
        followup=object(),
        interaction=SimpleNamespace(id=99, created_at=created_at),
        channel=None,
        guild_id=None,
        channel_id=123,
        author=SimpleNamespace(id=2, display_name="tester"),
        locale=None,
    )


@pytest.mark.asyncio
async def test_slow_but_successful_defer_still_dispatches_command():
    """Discord 可能已接受 ack 只是响应慢，回调不能在 3 秒内就放弃 defer。"""

    async def slow_defer(ephemeral: bool = False) -> None:
        await asyncio.sleep(2.6)

    adapter, handle_msg = _slash_adapter()
    ctx = _slash_ctx(slow_defer, discord.utils.utcnow())

    await adapter._create_dynamic_callback("ping", 0)(ctx)

    handle_msg.assert_awaited_once()
    assert handle_msg.await_args_list[0].args[1] is ctx.followup


@pytest.mark.asyncio
async def test_late_interaction_logs_age_at_callback(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(
        discord_platform_adapter.logger,
        "warning",
        lambda msg, *args, **kwargs: warnings.append(msg),
    )

    async def instant_defer(ephemeral: bool = False) -> None:
        return None

    adapter, handle_msg = _slash_adapter()
    ctx = _slash_ctx(instant_defer, discord.utils.utcnow() - timedelta(seconds=2))

    await adapter._create_dynamic_callback("ping", 0)(ctx)

    handle_msg.assert_awaited_once()
    assert any("Slow defer" in w and "age 2." in w for w in warnings), warnings
