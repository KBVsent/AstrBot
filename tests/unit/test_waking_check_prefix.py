from types import SimpleNamespace
from typing import Any, cast

import pytest

from astrbot.core.message.components import At, Plain
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


@pytest.mark.asyncio
async def test_adapter_awakened_mention_still_strips_wake_prefix(monkeypatch):
    """A Discord style "@bot /cmd" must reach command filters as "cmd"."""
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
        lambda *_args, **_kwargs: [],
    )
    config = {
        "admins_id": [],
        "wake_prefix": ["/"],
        "plugin_set": ["*"],
        "platform_settings": {},
    }
    stage = WakingCheckStage()
    await stage.initialize(
        cast(
            Any,
            SimpleNamespace(
                astrbot_config=config,
                astrbot_config_id="test-conf-id",
                db_helper=None,
                plugin_manager=SimpleNamespace(
                    context=SimpleNamespace(get_config=lambda **_: config)
                ),
            ),
        )
    )
    abm = AstrBotMessage()
    abm.type = MessageType.GROUP_MESSAGE
    abm.self_id = "bot"
    abm.session_id = abm.group_id = "channel"
    abm.message_id = "m1"
    abm.sender = MessageMember(user_id="u1", nickname="tester")
    abm.message = [Plain("/mt_cron status"), At(qq="bot")]
    abm.message_str = "/mt_cron status"
    abm.raw_message = None
    event = AstrMessageEvent(
        abm.message_str,
        abm,
        PlatformMetadata(name="discord", description="", id="dc"),
        "channel",
    )
    event.is_wake = True
    event.is_at_or_wake_command = True

    await stage.process(event)

    assert event.message_str == "mt_cron status"
    assert not event.is_stopped()
