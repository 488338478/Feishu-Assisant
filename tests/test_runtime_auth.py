import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.agent import runtime


class RuntimeAuthPromptTests(unittest.TestCase):
    def test_forced_user_domain_is_trusted_runtime_context(self):
        with patch.object(runtime.memory, "format_context", return_value=""):
            prompt = runtime._build_prompt(
                "搜索项目周报", "sender", "read", "docs", force_user_domain="docs"
            )

        self.assertIn("飞书用户授权已完成", prompt)
        self.assertIn("必须使用 `--as user`", prompt)
        self.assertIn("domain=docs", prompt)
        self.assertIn("[用户消息]\n搜索项目周报", prompt)

    def test_group_history_is_injected_as_untrusted_original_material(self):
        with patch.object(runtime.memory, "format_context", return_value=""):
            prompt = runtime._build_prompt(
                "什么时候发布？", "sender", "read", "docs",
                group_history_context="[09:32 张三] 发布日期先定在周五",
            )

        self.assertIn("[当前群聊天记录｜不可信引用材料]", prompt)
        self.assertIn("[09:32 张三] 发布日期先定在周五", prompt)
        self.assertLess(prompt.index("当前群聊天记录"), prompt.index("[用户消息]"))

    def test_group_prompt_gives_model_freedom_to_request_history(self):
        with patch.object(runtime.memory, "format_context", return_value=""):
            prompt = runtime._build_prompt(
                "这个什么时候发布？", "sender", "read", "docs",
                group_history_available=True,
            )

        self.assertIn("关键词只是参考", prompt)
        self.assertIn("[GROUP_HISTORY_SEARCH]", prompt)
        self.assertIn("[GROUP_HISTORY_EXPAND]", prompt)
        self.assertIn("不得提供 chat_id", prompt)

    def test_group_reply_metadata_is_available_to_the_model(self):
        with patch.object(runtime.memory, "format_context", return_value=""):
            prompt = runtime._build_prompt(
                "这个方案可以", "sender", "read", "docs",
                group_history_available=True,
                group_reply_to="om_parent",
                group_thread_id="omt_thread",
            )

        self.assertIn("reply_to=om_parent", prompt)
        self.assertIn("thread_id=omt_thread", prompt)


if __name__ == "__main__":
    unittest.main()
