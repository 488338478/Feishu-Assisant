import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant import handlers


class SessionControlTests(unittest.TestCase):
    def test_question_about_clearing_context_returns_command_guidance(self):
        with (
            patch.object(handlers.runtime, "run") as run,
            patch.object(handlers.scheduler, "configure", return_value=None),
        ):
            result = handlers.process_message(
                "怎么清除之前的上下文？", "chat", "user", object()
            )

        self.assertIn("发送「清除上下文」", result)
        run.assert_not_called()

    def test_new_topic_intent_guides_without_clearing_automatically(self):
        with (
            patch.object(handlers.runtime, "run") as run,
            patch.object(handlers.runtime, "clear_session") as clear,
            patch.object(handlers.scheduler, "configure", return_value=None),
        ):
            result = handlers.process_message(
                "我想换个话题重新开始", "group-1", "user-a", object(), chat_type="group"
            )

        self.assertIn("新建会话", result)
        self.assertIn("该群共享", result)
        clear.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
