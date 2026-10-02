import sys
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.agent.permission import (
    decision_from_text,
    is_admin,
    question_answer_from_text,
    session_allow_pattern,
)
from assistant.agent import permission_mcp, runtime


class PermissionApprovalTests(unittest.TestCase):
    def test_only_configured_admin_ids_can_approve(self):
        self.assertTrue(is_admin("ou_admin", ["ou_admin"]))
        self.assertFalse(is_admin("ou_member", ["ou_admin"]))
        self.assertFalse(is_admin("", ["ou_admin"]))

    def test_decision_parser_accepts_only_explicit_commands(self):
        self.assertEqual(decision_from_text("允许一次"), "once")
        self.assertEqual(decision_from_text("允许本次会话"), "session")
        self.assertEqual(decision_from_text("拒绝"), "deny")
        self.assertIsNone(decision_from_text("允许一下吧"))

    def test_question_answer_parser_requires_explicit_nonempty_answer(self):
        self.assertEqual(question_answer_from_text("回答：用 PostgreSQL"), "用 PostgreSQL")
        self.assertEqual(question_answer_from_text(" 回答: A, B "), "A, B")
        self.assertIsNone(question_answer_from_text("我觉得选 A"))
        self.assertIsNone(question_answer_from_text("回答：   "))

    def test_session_allow_pattern_is_narrow_for_shell_commands(self):
        self.assertEqual(
            session_allow_pattern("Bash", {"command": "p4 client -o agent_workspace"}),
            "Bash(p4 client -o:*)",
        )
        self.assertEqual(
            session_allow_pattern("mcp__unreal__get_actor", {}),
            "mcp__unreal__get_actor",
        )

    def test_approval_requires_admin_and_writes_decision_for_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = {
                "request": {
                    "token": "abc",
                    "chat_id": "chat",
                    "sender_id": "ou_requester",
                    "tool_name": "mcp__unreal__get_actor",
                    "tool_input": {},
                    "created_at": 1,
                },
                "directory": directory,
            }
            with runtime._permission_lock:
                runtime._pending_permissions["abc"] = pending
            self.addCleanup(lambda: runtime._pending_permissions.pop("abc", None))
            with patch.object(runtime, "_load_runtime_config", return_value={"admin_users": ["ou_admin"]}):
                self.assertEqual(
                    runtime.approve_permission("chat", "ou_member", "p2p", "once"),
                    "你没有权限确认工具调用。",
                )
                self.assertEqual(
                    runtime.approve_permission("chat", "ou_admin", "p2p", "once"),
                    "已允许本次工具调用。",
                )
            response = json.loads((Path(directory) / "response-abc.json").read_text(encoding="utf-8"))
            self.assertEqual(response["decision"], "allow")

    def test_admin_user_gets_explicit_bypass_mode_and_members_do_not(self):
        with patch.object(runtime, "_load_runtime_config", return_value={"admin_users": ["ou_admin"]}):
            self.assertEqual(runtime.permission_mode("ou_admin"), ("admin", [], True))
            self.assertEqual(runtime.permission_mode("ou_member"), ("read", runtime.TIER_DENY["read"], False))

    def test_question_prompt_shows_questions_and_choices_instead_of_admin_approval(self):
        message = runtime.permission_prompt_message({
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [{
                    "header": "数据库",
                    "question": "要使用哪个数据库？",
                    "options": [
                        {"label": "PostgreSQL", "description": "关系型数据库"},
                        {"label": "SQLite", "description": "单文件数据库"},
                    ],
                    "multiSelect": False,
                }],
            },
        })

        self.assertIn("要使用哪个数据库？", message)
        self.assertIn("PostgreSQL", message)
        self.assertIn("回答：", message)
        self.assertNotIn("仅管理员可确认", message)

    def test_only_original_requester_can_answer_question_and_answer_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {
                "token": "question-token",
                "chat_id": "chat",
                "sender_id": "ou_requester",
                "chat_type": "group",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [{
                        "header": "数据库",
                        "question": "要使用哪个数据库？",
                        "options": [
                            {"label": "PostgreSQL", "description": "关系型"},
                            {"label": "SQLite", "description": "单文件"},
                        ],
                        "multiSelect": False,
                    }],
                    "metadata": {"source": "claude"},
                },
                "created_at": 1,
            }
            with runtime._permission_lock:
                runtime._pending_permissions["question-token"] = {
                    "request": request,
                    "directory": directory,
                }
            self.addCleanup(lambda: runtime._pending_permissions.pop("question-token", None))

            self.assertEqual(
                runtime.answer_user_question("chat", "ou_other", "group", "PostgreSQL"),
                "当前没有等待你回答的 Claude 问题。",
            )
            self.assertEqual(
                runtime.answer_user_question("chat", "ou_requester", "group", "PostgreSQL"),
                "已提交回答，Claude 正在继续处理。",
            )

            response = json.loads(
                (Path(directory) / "response-question-token.json").read_text(encoding="utf-8")
            )
            self.assertEqual(response["decision"], "allow")
            self.assertEqual(response["updated_input"]["questions"], request["tool_input"]["questions"])
            self.assertEqual(response["updated_input"]["response"], "PostgreSQL")
            self.assertNotIn("metadata", response["updated_input"])

    def test_admin_approval_does_not_consume_pending_user_question(self):
        with tempfile.TemporaryDirectory() as directory:
            for token, tool_name, created_at in (
                ("tool-token", "Bash", 1),
                ("question-token", "AskUserQuestion", 2),
            ):
                with runtime._permission_lock:
                    runtime._pending_permissions[token] = {
                        "request": {
                            "token": token,
                            "chat_id": "chat",
                            "sender_id": "ou_requester",
                            "chat_type": "group",
                            "tool_name": tool_name,
                            "tool_input": {},
                            "created_at": created_at,
                        },
                        "directory": directory,
                    }
                self.addCleanup(lambda value=token: runtime._pending_permissions.pop(value, None))

            with patch.object(runtime, "_load_runtime_config", return_value={"admin_users": ["ou_admin"]}):
                result = runtime.approve_permission("chat", "ou_admin", "group", "once")

            self.assertEqual(result, "已允许本次工具调用。")
            self.assertTrue((Path(directory) / "response-tool-token.json").exists())
            self.assertFalse((Path(directory) / "response-question-token.json").exists())

    def test_permission_mcp_forwards_question_answer_as_updated_input(self):
        updated_input = {
            "questions": [{"question": "选哪个？"}],
            "response": "选 A",
        }

        self.assertEqual(
            permission_mcp._permission_result({
                "decision": "allow", "updated_input": updated_input,
            }),
            {"behavior": "allow", "updatedInput": updated_input},
        )

    def test_invalid_empty_question_is_denied_without_waiting_for_user(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict("os.environ", {"ASSISTANT_PERMISSION_DIR": directory}), \
             patch.object(permission_mcp.time, "time", side_effect=[0, 0, 301]):
            result = permission_mcp._wait_for_decision(
                {"tool_name": "AskUserQuestion", "tool_input": {}}
            )

            self.assertEqual(result["behavior"], "deny")
            self.assertIn("questions", result["message"])
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
