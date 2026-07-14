"""Tests for inbound message log classification."""

from unittest.mock import MagicMock, patch

from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

_STAGE_MODULE = "astrbot.core.pipeline.waking_check.stage"


def _create_stage() -> WakingCheckStage:
    stage = WakingCheckStage()
    stage.ctx = MagicMock()
    return stage


def _patch_log_mode(log_mode: str = "wake_only"):
    """Patch the default config that the stage reads inbound_message_log from."""
    return patch.dict(
        f"{_STAGE_MODULE}.default_astrbot_config",
        {"inbound_message_log": log_mode},
        clear=False,
    )


def _create_event(
    sender_name: str | None = "TestUser",
    *,
    private: bool = True,
    group_id: str = "789012",
    group_name: str | None = "测试群",
) -> MagicMock:
    event = MagicMock()
    event.get_extra.return_value = "TestConfig"
    event.get_platform_id.return_value = "test-platform"
    event.get_platform_name.return_value = "Test Platform"
    event.get_sender_name.return_value = sender_name
    event.get_sender_id.return_value = "user123"
    event.get_message_outline.return_value = "Hello"
    event.is_private_chat.return_value = private
    event.get_group_id.return_value = "" if private else group_id
    if private:
        event.message_obj.group = None
    else:
        event.message_obj.group = MagicMock(group_name=group_name)
    return event


def test_important_inbound_message_uses_info() -> None:
    """Important inbound messages should remain visible at INFO."""
    stage = _create_stage()
    event = _create_event()

    with _patch_log_mode(), patch(f"{_STAGE_MODULE}.logger") as mock_logger:
        stage._log_inbound_event(event, important=True)

    mock_logger.info.assert_called_once()
    mock_logger.debug.assert_not_called()
    message = mock_logger.info.call_args.args[0]
    assert "TestConfig" in message
    assert "[私聊]" in message
    assert "TestUser/user123" in message
    assert "Hello" in message


def test_unimportant_inbound_message_uses_debug() -> None:
    """Unimportant inbound messages should remain available at DEBUG."""
    stage = _create_stage()
    event = _create_event(sender_name=None)

    with _patch_log_mode(), patch(f"{_STAGE_MODULE}.logger") as mock_logger:
        stage._log_inbound_event(event, important=False)

    mock_logger.debug.assert_called_once()
    mock_logger.info.assert_not_called()
    message = mock_logger.debug.call_args.args[0]
    assert "[私聊]" in message
    assert "user123: Hello" in message


def test_group_inbound_message_includes_group_name_and_id() -> None:
    """Group chat logs should include group name and group id."""
    stage = _create_stage()
    event = _create_event(private=False, group_id="789012", group_name="测试群")

    with _patch_log_mode(), patch(f"{_STAGE_MODULE}.logger") as mock_logger:
        stage._log_inbound_event(event, important=True)

    message = mock_logger.info.call_args.args[0]
    assert "[群聊 测试群(789012)]" in message


def test_group_inbound_message_without_group_name() -> None:
    """Group chat logs should still show group id when name is missing."""
    stage = _create_stage()
    event = _create_event(private=False, group_id="789012", group_name="N/A")

    with _patch_log_mode(), patch(f"{_STAGE_MODULE}.logger") as mock_logger:
        stage._log_inbound_event(event, important=True)

    message = mock_logger.info.call_args.args[0]
    assert "[群聊 (789012)]" in message


def test_all_mode_keeps_unimportant_messages_at_info() -> None:
    """The all mode should preserve the previous INFO behavior."""
    stage = _create_stage()
    event = _create_event()

    with _patch_log_mode("all"), patch(f"{_STAGE_MODULE}.logger") as mock_logger:
        stage._log_inbound_event(event, important=False)

    mock_logger.info.assert_called_once()
    mock_logger.debug.assert_not_called()
