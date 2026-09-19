import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant import handlers

REAL_RUNTIME_RUN = handlers.runtime.run


class DevProfileRoutingTests(unittest.TestCase):
    def setUp(self):
        self.patches = (
            patch.object(handlers.scheduler, "configure", return_value=None),
            patch.object(handlers.memory, "auto_learn"),
            patch.object(
                handlers.runtime,
                "run",
                return_value=("仓库概览", {"ok": True, "tools": 1, "duration": 0.1}),
            ),
        )
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_p4v_request_in_docs_profile_is_blocked_before_agent(self):
        with patch.object(handlers.runtime, "resolve_profile", return_value="docs"):
            result = handlers.process_message(
                "进入p4v看看有啥", "oc_dev_group", "ou_user", object(),
                chat_type="group",
            )

        self.assertIn("当前会话未配置为研发模式", result)
        self.assertIn("P4_WORKSPACE", result)
        self.assertIn("runtime_config.json", result)
        handlers.runtime.run.assert_not_called()

    def test_code_repository_correction_in_docs_profile_is_also_blocked(self):
        with patch.object(handlers.runtime, "resolve_profile", return_value="docs"):
            result = handlers.process_message(
                "我的意思是你去看代码仓库不是看飞书",
                "oc_dev_group", "ou_user", object(), chat_type="group",
            )

        self.assertIn("当前会话未配置为研发模式", result)
        handlers.runtime.run.assert_not_called()

    def test_p4v_request_in_dev_profile_reaches_agent(self):
        with patch.object(handlers.runtime, "resolve_profile", return_value="dev"):
            result = handlers.process_message(
                "进入p4v看看有啥", "oc_dev_group", "ou_user", object(),
                chat_type="group",
            )

        self.assertEqual(result, "仓库概览")
        handlers.runtime.run.assert_called_once()

    def test_explicit_feishu_p4_document_request_stays_in_docs_profile(self):
        with patch.object(handlers.runtime, "resolve_profile", return_value="docs"):
            result = handlers.process_message(
                "搜索飞书知识库里的 P4 使用说明", "oc_docs", "ou_user", object(),
            )

        self.assertEqual(result, "仓库概览")
        handlers.runtime.run.assert_called_once()

    def test_dev_profile_without_workspace_names_required_setting(self):
        with patch.object(handlers.runtime, "_profile_cwd", return_value=None):
            result, stats = REAL_RUNTIME_RUN(
                "oc_dev_group", "查看仓库", sender_id="ou_user", profile="dev"
            )

        self.assertIn("P4_WORKSPACE", result)
        self.assertFalse(stats["ok"])

    def test_writable_tier_fails_before_agent_when_systemd_mount_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(handlers.runtime, "_profile_cwd", return_value=Path(tmp)), \
             patch.object(handlers.runtime, "resolve_tier", return_value="submit"), \
             patch.object(handlers.runtime.os, "statvfs", return_value=Mock(f_flag=1), create=True), \
             patch.object(handlers.runtime.os, "ST_RDONLY", 1, create=True), \
             patch.object(handlers.runtime, "_run_claude") as run_claude:
            result, stats = REAL_RUNTIME_RUN(
                "oc_dev_group", "创建测试文件", sender_id="ou_user", profile="dev"
            )

        self.assertIn("ReadWritePaths", result)
        self.assertIn("只读", result)
        self.assertFalse(stats["ok"])
        run_claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
