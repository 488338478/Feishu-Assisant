import sys
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.lark_command import resolve_lark_cli


class ResolveLarkCliTests(unittest.TestCase):
    def test_uses_resolved_windows_cmd_wrapper(self):
        expected = r"C:\Users\test\AppData\Roaming\npm\lark-cli.cmd"

        actual = resolve_lark_cli(which=lambda _: expected)

        self.assertEqual(actual, expected)

    def test_falls_back_to_command_name_when_not_resolved(self):
        self.assertEqual(resolve_lark_cli(which=lambda _: None), "lark-cli")


if __name__ == "__main__":
    unittest.main()
