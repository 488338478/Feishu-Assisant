import sys
import json
import tempfile
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

    def test_shared_document_read_does_not_preflight_auth(self):
        manager = MagicMock()
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "读取项目周报", "chat", "user", self.client
            )

        self.assertEqual(result, "完成")
        manager.ensure_user.assert_not_called()

    def test_shared_document_search_does_not_preflight_auth(self):
        manager = MagicMock()
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "找一下总策划文档并总结", "chat", "user", self.client
            )

        self.assertEqual(result, "完成")
        manager.ensure_user.assert_not_called()
        self.assertIsNone(
            handlers.runtime.run.call_args.kwargs["force_user_domain"]
        )

    def test_personal_document_search_authorizes_before_runtime(self):
        manager = MagicMock()
        manager.ensure_user.return_value = AuthResult(True)
        with patch.object(handlers, "auth_manager", manager):
            result = handlers.process_message(
                "找一下我创建的总策划文档并总结", "chat", "user", self.client
            )

        self.assertEqual(result, "完成")
        self.assertEqual(manager.ensure_user.call_args.args[0], "docs")
        self.assertEqual(
            handlers.runtime.run.call_args.kwargs["force_user_domain"], "docs"
        )

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
        self.assertEqual(
            handlers.runtime.run.call_args.kwargs["force_user_domain"], "calendar"
        )

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
                "读取项目周报", "chat", "user", self.client
            )

        self.assertEqual(result, "已找到文档")
        manager.ensure_user.assert_called_once()
        self.assertEqual(handlers.runtime.run.call_count, 2)
        retry_call = handlers.runtime.run.call_args_list[1]
        self.assertEqual(retry_call.args[1], "读取项目周报")
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
                "读取项目周报", "chat", "user", self.client
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
                "读取项目周报", "chat", "user", self.client
            )

        self.assertNotIn("LARK_USER_AUTH_REQUIRED", result)
        self.assertIn("授权已完成", result)
        self.assertEqual(handlers.runtime.run.call_count, 2)

    def test_explicit_question_answer_resumes_pending_claude_call_without_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {
                "token": "question-token",
                "chat_id": "chat",
                "sender_id": "user",
                "chat_type": "p2p",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [{
                        "header": "格式",
                        "question": "使用哪种格式？",
                        "options": [
                            {"label": "摘要", "description": "简短"},
                            {"label": "详细", "description": "完整"},
                        ],
                        "multiSelect": False,
                    }],
                },
                "created_at": 1,
            }
            with handlers.runtime._permission_lock:
                handlers.runtime._pending_permissions["question-token"] = {
                    "request": request,
                    "directory": directory,
                }
            self.addCleanup(
                lambda: handlers.runtime._pending_permissions.pop("question-token", None)
            )

            result = handlers.process_message(
                "回答：摘要", "chat", "user", self.client, chat_type="p2p"
            )

            self.assertEqual(result, "已提交回答，Claude 正在继续处理。")
            handlers.runtime.run.assert_not_called()
            response = json.loads(
                (Path(directory) / "response-question-token.json").read_text(encoding="utf-8")
            )
            self.assertEqual(response["updated_input"]["response"], "摘要")


if __name__ == "__main__":
    unittest.main()
