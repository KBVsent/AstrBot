from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from astrbot.core.pipeline.waking_check.stage import (
    WakingCheckStage,
    star_handlers_registry,
)
from astrbot.core.star.filter.callback import CallbackFilter


@pytest.mark.asyncio
async def test_native_callback_only_activates_matching_callback_handlers(monkeypatch):
    """A native callback must not reach message handlers or other plugins' prefixes."""
    config = {"admins_id": [], "wake_prefix": [], "plugin_set": ["*"]}
    stage = WakingCheckStage()
    stage.ctx = cast(
        Any,
        SimpleNamespace(
            astrbot_config=config,
            plugin_manager=SimpleNamespace(
                context=SimpleNamespace(get_config=lambda **_: config)
            ),
        ),
    )
    stage.ignore_bot_self_message = False
    stage.friend_message_needs_wake_prefix = False
    stage.ignore_at_all = False
    stage.disable_builtin_commands = False
    stage._umo_auto_name_recorder = MagicMock()

    any_message_filter = MagicMock()
    any_message_filter.filter.return_value = True

    def handler(name, event_filter):
        return SimpleNamespace(
            event_filters=[event_filter],
            handler_module_path=f"test.{name}",
            handler_full_name=f"test.{name}.handler",
        )

    matching = handler("mine", CallbackFilter("mcmai_"))
    monkeypatch.setattr(
        star_handlers_registry,
        "get_handlers_by_event_type",
        lambda *_args, **_kwargs: [
            handler("ordinary", any_message_filter),
            handler("other", CallbackFilter("other_")),
            matching,
        ],
    )

    event = MagicMock()
    event.message_str = ""
    event.plugins_name = None
    event.is_wake = False
    event.is_at_or_wake_command = False
    event.get_messages.return_value = []
    event.get_extra.side_effect = lambda _key=None, default=None: default
    event.get_button_interaction.return_value = None
    event.is_button_interaction.return_value = False
    event.is_callback_event.return_value = True
    event.get_callback_data.return_value = "mcmai_page|2"
    event.is_private_chat.return_value = True

    await stage.process(event)

    any_message_filter.filter.assert_not_called()
    activated = next(
        call.args[1]
        for call in event.set_extra.call_args_list
        if call.args[0] == "activated_handlers"
    )
    assert activated == [matching]
    assert event.is_at_or_wake_command is False
