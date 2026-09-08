import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.auth import AuthResult
from assistant import handlers


class HandlerAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = object()
        self.common = (
            patch.object(handlers.scheduler, "configure", return_value=None),
            patch.object(handlers.memory, "auto_learn"),
            patch.object(
                handlers.runtime,
                "run",
                return_value=("完成", {"ok": True, "tools": 0, "duration": 0.1}),
            ),
            patch.object(handlers, "send_message"),
        )
        for item in self.common:
            item.start()
            self.addCleanup(item.stop)

    def test_bot_first_message_does_not_preflight_auth(self):
        manager = MagicMock()
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "搜索项目周报", "chat", "user", self.client
            )

        self.assertEqual(result, "完成")
        manager.ensure_user.assert_not_called()

    def test_personal_request_authorizes_before_runtime(self):
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(True)
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "查看我的日程", "chat", "user", self.client
            )

        self.assertEqual(result, "完成")
        manager.ensure_user.assert_called_once()
        handlers.runtime.run.assert_called_once()

    def test_auth_link_is_sent_to_triggering_chat(self):
        manager = MagicMock()

        def authorize(domain, notify):
            notify("https://example.test/verify")
            return AuthResult(True)

        manager.ensure_user.side_effect = authorize
        with patch.object(handlers, "auth_manager", manager):
            handlers.process_message("查看我的日程", "chat-123", "user", self.client)

        handlers.send_message.assert_called_once_with(
            self.client, "chat-123", "https://example.test/verify"
        )

    def test_failed_authorization_does_not_run_claude(self):
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(False, "授权已过期")
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "查看我的日程", "chat", "user", self.client
            )

        self.assertEqual(result, "授权已过期")
        handlers.runtime.run.assert_not_called()

    def test_bot_auth_marker_authorizes_and_retries_once(self):
        handlers.runtime.run.side_effect = [
            (
                "[LARK_USER_AUTH_REQUIRED:docs]",
                {"ok": True, "tools": 1, "duration": 0.2},
            ),
            ("已找到文档", {"ok": True, "tools": 1, "duration": 0.3}),
        ]
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(True)
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "搜索项目周报", "chat", "user", self.client
            )

        self.assertEqual(result, "已找到文档")
        manager.ensure_user.assert_called_once()
        self.assertEqual(handlers.runtime.run.call_count, 2)
        retry_call = handlers.runtime.run.call_args_list[1]
        self.assertEqual(retry_call.args[1], "搜索项目周报")
        self.assertEqual(retry_call.kwargs["force_user_domain"], "docs")

    def test_auth_marker_failure_does_not_retry_forever(self):
        handlers.runtime.run.return_value = (
            "[LARK_USER_AUTH_REQUIRED:docs]",
            {"ok": True, "tools": 1, "duration": 0.2},
        )
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(False, "授权失败")
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "搜索项目周报", "chat", "user", self.client
            )

        self.assertEqual(result, "授权失败")
        self.assertEqual(handlers.runtime.run.call_count, 1)

    def test_second_auth_marker_returns_clear_error_not_internal_marker(self):
        handlers.runtime.run.return_value = (
            "[LARK_USER_AUTH_REQUIRED:docs]",
            {"ok": True, "tools": 1, "duration": 0.2},
        )
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(True)
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "搜索项目周报", "chat", "user", self.client
            )

        self.assertNotIn("LARK_USER_AUTH_REQUIRED", result)
        self.assertIn("授权已完成", result)
        self.assertEqual(handlers.runtime.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
