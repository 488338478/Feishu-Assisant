import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

pkg = types.ModuleType("assistant")
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", pkg)

from assistant.agent import runtime
from assistant import handlers


class ContextCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions_path = Path(self.tmp.name) / "sessions.json"
        self.sessions_patch = patch.object(runtime, "SESSIONS_FILE", self.sessions_path)
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)
        runtime.sessions.clear()
        runtime._chat_locks.clear()

    def test_run_requests_confirmation_before_over_budget_request(self):
        scope = runtime.conversation_key("compact-chat", "user", "p2p", "docs")
        runtime.sessions[scope] = {
            "active": "sid",
            "history": [],
            "estimated_chars": 99,
        }
        with patch.object(runtime, "CONTEXT_COMPACTION_THRESHOLD_CHARS", 100), \
             patch.object(runtime, "_profile_cwd", return_value=Path(self.tmp.name)), \
             patch.object(runtime, "resolve_profile", return_value="docs"), \
             patch.object(runtime, "resolve_tier", return_value="read"), \
             patch.object(runtime, "resolve_model", return_value=runtime.DEFAULT_MODEL), \
             patch.object(runtime, "_run_claude") as run_claude:
            response, stats = runtime.run("compact-chat", "继续画", sender_id="user")

        self.assertIn("确认压缩", response)
        self.assertTrue(stats["needs_compaction"])
        run_claude.assert_not_called()
        self.assertEqual(runtime.sessions[scope]["pending_compaction"]["text"], "继续画")

    def test_cancel_compaction_discards_pending_request_without_calling_claude(self):
        scope = runtime.conversation_key("compact-chat", "user", "p2p", "docs")
        runtime.sessions[scope] = {
            "active": "sid",
            "history": [],
            "estimated_chars": 99,
            "pending_compaction": {"text": "继续画", "sender_id": "user"},
        }
        response, stats = runtime.cancel_compaction("compact-chat", sender_id="user")

        self.assertIn("取消", response)
        self.assertFalse(stats["pending"])
        self.assertNotIn("pending_compaction", runtime.sessions[scope])
        self.assertEqual(runtime.sessions[scope]["active"], "sid")

    def test_confirm_compacts_then_replays_original_request_once(self):
        scope = runtime.conversation_key("compact-chat", "user", "p2p", "docs")
        runtime.sessions[scope] = {
            "active": "sid",
            "history": [],
            "estimated_chars": 99,
            "pending_compaction": {
                "chat_id": "compact-chat",
                "text": "继续画",
                "sender_id": "user",
                "profile": "docs",
                "chat_type": "p2p",
            },
        }
        with patch.object(runtime, "_compact_session", return_value=("compact-sid", 1)) as compact, \
             patch.object(runtime, "_run_claude", return_value=("完成", "final-sid", 1)) as run_claude, \
             patch.object(runtime, "_profile_cwd", return_value=Path(self.tmp.name)), \
             patch.object(runtime, "resolve_profile", return_value="docs"), \
             patch.object(runtime, "resolve_tier", return_value="read"), \
             patch.object(runtime, "resolve_model", return_value=runtime.DEFAULT_MODEL), \
             patch.object(runtime, "_audit"):
            response, stats = runtime.confirm_compaction("compact-chat", sender_id="user")

        self.assertEqual(response, "完成")
        self.assertTrue(stats["compacted"])
        compact.assert_called_once()
        run_claude.assert_called_once()
        self.assertEqual(runtime.sessions[scope]["active"], "final-sid")
        self.assertNotIn("pending_compaction", runtime.sessions[scope])

    def test_compact_session_uses_claude_compact_command(self):
        cwd = Path(self.tmp.name)
        with patch.object(runtime, "_run_claude", return_value=("压缩完成", "new-sid", 2)) as run:
            result = runtime._compact_session("sid", cwd, ["Edit"], 30, "model")

        self.assertEqual(result, ("new-sid", 2))
        run.assert_called_once_with("/compact", "sid", cwd, ["Edit"], 30, model="model")

    def test_handler_confirmation_commands_are_deterministic(self):
        with patch.object(handlers.runtime, "has_pending_compaction", return_value=False), \
             patch.object(handlers.runtime, "confirm_compaction", return_value=("继续完成", {})) as confirm, \
             patch.object(handlers.scheduler, "configure", return_value=None):
            response = handlers.process_message("确认压缩", "compact-chat", "user", object())

        self.assertEqual(response, "继续完成")
        confirm.assert_called_once_with("compact-chat", sender_id="user", chat_type="p2p")

    def test_handler_blocks_new_request_until_compaction_decision(self):
        with patch.object(handlers.runtime, "has_pending_compaction", return_value=True), \
             patch.object(handlers.runtime, "run") as run:
            response = handlers.process_message("继续画", "compact-chat", "user", object())

        self.assertIn("确认压缩", response)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
