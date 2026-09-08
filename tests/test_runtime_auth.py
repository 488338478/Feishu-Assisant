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


if __name__ == "__main__":
    unittest.main()
