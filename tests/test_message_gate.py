import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.message_gate import message_targets_bot, strip_bot_mention
from assistant import handlers


def mention(open_id, key="@_user_1"):
    return SimpleNamespace(id=SimpleNamespace(open_id=open_id), key=key)


def event(chat_type, mentions=None, text="你好", message_type="text", raw_content=None):
    message = SimpleNamespace(
        chat_type=chat_type,
        mentions=mentions,
        chat_id="chat",
        message_id="message",
        create_time="1757640720000",
        parent_id="",
        root_id="",
        message_type=message_type,
        content=raw_content if raw_content is not None else json.dumps({"text": text}, ensure_ascii=False),
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="sender"))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


class MessageGateTests(unittest.TestCase):
    def test_private_message_needs_no_mention(self):
        self.assertTrue(message_targets_bot("p2p", None, "bot-id"))

    def test_group_message_without_mention_is_ignored(self):
        self.assertFalse(message_targets_bot("group", None, "bot-id"))

    def test_group_mentioning_another_user_is_ignored(self):
        self.assertFalse(
            message_targets_bot("group", [mention("other-id")], "bot-id")
        )

    def test_group_mentioning_current_bot_is_accepted(self):
        self.assertTrue(
            message_targets_bot("group", [mention("bot-id")], "bot-id")
        )

    def test_missing_bot_identity_fails_closed_for_group(self):
        self.assertFalse(message_targets_bot("group", [mention("bot-id")], ""))

    def test_bot_mention_placeholder_is_removed_from_text(self):
        mentions = [mention("bot-id", "@_user_1"), mention("other-id", "@_user_2")]
        self.assertEqual(
            strip_bot_mention("@_user_1 帮我问 @_user_2", mentions, "bot-id"),
            "帮我问 @_user_2",
        )


class HandlerGateTests(unittest.TestCase):
    def test_unmentioned_group_message_is_captured_without_starting_ai(self):
        with (
            patch.object(handlers, "BOT_OPEN_ID", "bot-id"),
            patch.object(handlers.group_history_store, "record") as record,
            patch.object(handlers, "build_client") as build_client,
            patch.object(handlers.threading, "Thread") as thread,
        ):
            handlers.on_message(event("group", [], "项目周五发布"))

        record.assert_called_once()
        self.assertEqual(record.call_args.args[0].content, "项目周五发布")
        build_client.assert_not_called()
        thread.assert_not_called()

    def test_private_message_still_starts_processing(self):
        with (
            patch.object(handlers, "BOT_OPEN_ID", "bot-id"),
            patch.object(handlers, "build_client", return_value=MagicMock()),
            patch.object(handlers.threading, "Thread") as thread,
        ):
            handlers.on_message(event("p2p"))

        thread.assert_called_once()

    def test_mentioned_group_message_starts_processing(self):
        with (
            patch.object(handlers, "BOT_OPEN_ID", "bot-id"),
            patch.object(handlers, "build_client", return_value=MagicMock()),
            patch.object(handlers.threading, "Thread") as thread,
        ):
            handlers.on_message(
                event("group", [mention("bot-id")], "@_user_1 总结文档")
            )

        thread.assert_called_once()

    def test_non_text_group_message_preserves_raw_event_content(self):
        raw = '{"title":"构建日志","elements":[{"tag":"text","text":"ERROR  raw"}]}'
        with (
            patch.object(handlers, "BOT_OPEN_ID", "bot-id"),
            patch.object(handlers.group_history_store, "record") as record,
            patch.object(handlers.threading, "Thread") as thread,
        ):
            handlers.on_message(event(
                "group", [], message_type="post", raw_content=raw
            ))

        self.assertEqual(record.call_args.args[0].content, raw)
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
