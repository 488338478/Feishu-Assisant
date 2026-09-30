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
    session_allow_pattern,
)
from assistant.agent import runtime


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


if __name__ == "__main__":
    unittest.main()
