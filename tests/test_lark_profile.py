import subprocess
import sys
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.lark_profile import sync_lark_profile


class SyncLarkProfileTests(unittest.TestCase):
    def test_sync_uses_stdin_for_secret_and_never_argv(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        sync_lark_profile(
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-value",
                "LARK_CLI_PROFILE": "assistant-bot",
            },
            fake_run,
        )

        argv, kwargs = calls[0]
        self.assertNotIn("secret-value", argv)
        self.assertEqual(kwargs["input"], "secret-value\n")
        self.assertEqual(
            argv,
            [
                "lark-cli",
                "config",
                "init",
                "--name",
                "assistant-bot",
                "--app-id",
                "cli_test",
                "--app-secret-stdin",
                "--brand",
                "feishu",
            ],
        )

    def test_sync_rejects_missing_credentials_without_running_cli(self):
        def unexpected_run(*args, **kwargs):
            self.fail("lark-cli must not run without credentials")

        with self.assertRaisesRegex(RuntimeError, "missing Feishu credentials"):
            sync_lark_profile({}, unexpected_run)

    def test_sync_redacts_secret_from_cli_failure(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 1, "", f"invalid secret: {kwargs['input'].strip()}"
            )

        with self.assertRaises(RuntimeError) as raised:
            sync_lark_profile(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret-value",
                    "LARK_CLI_PROFILE": "assistant-bot",
                },
                fake_run,
            )

        self.assertNotIn("secret-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
