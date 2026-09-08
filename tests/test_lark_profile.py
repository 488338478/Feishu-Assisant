import importlib
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
            executable="lark-cli",
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
            sync_lark_profile({}, unexpected_run, executable="lark-cli")

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
                executable="lark-cli",
            )

        self.assertNotIn("secret-value", str(raised.exception))

    def test_sync_redacts_secret_before_truncating_cli_failure(self):
        secret = "LEAKMARKER"

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "x" * 295 + secret)

        with self.assertRaises(RuntimeError) as raised:
            sync_lark_profile(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": secret,
                    "LARK_CLI_PROFILE": "assistant-bot",
                },
                fake_run,
                executable="lark-cli",
            )

        self.assertNotIn(secret[:5], str(raised.exception))


class StartupOrderTests(unittest.TestCase):
    def test_profile_sync_precedes_scheduler_and_websocket(self):
        main_module = importlib.import_module("assistant.main")
        events = []

        class FakeScheduler:
            config = {"enabled": True}

            def set_push_callback(self, callback):
                self.push_callback = callback

            def start(self):
                events.append("scheduler")

            def stop(self):
                events.append("scheduler-stop")

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        class FakeHandlerBuilder:
            def __getattr__(self, name):
                if name.startswith("register_"):
                    return lambda *args: self
                raise AttributeError(name)

            def build(self):
                return object()

        class FakeWebSocketClient:
            def start(self):
                events.append("websocket")

        fake_scheduler = FakeScheduler()
        with (
            patch.object(main_module, "APP_ID", "cli_test"),
            patch.object(main_module, "APP_SECRET", "test-secret"),
            patch.object(
                main_module,
                "sync_lark_profile",
                side_effect=lambda env: events.append("profile-sync"),
            ),
            patch.object(main_module, "scheduler", fake_scheduler),
            patch.object(main_module.threading, "Thread", FakeThread),
            patch.object(
                main_module.lark.EventDispatcherHandler,
                "builder",
                return_value=FakeHandlerBuilder(),
            ),
            patch.object(
                main_module.lark.ws,
                "Client",
                return_value=FakeWebSocketClient(),
            ),
            patch("builtins.print"),
        ):
            main_module.main()

        self.assertEqual(events[:3], ["profile-sync", "scheduler", "websocket"])


if __name__ == "__main__":
    unittest.main()
