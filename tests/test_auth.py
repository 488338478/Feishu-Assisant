import json
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.auth import AuthResult, LarkAuthManager


class QueueRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if callable(result):
            return result(argv, kwargs)
        return result


def completed(payload, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class LarkAuthManagerTests(unittest.TestCase):
    def test_existing_user_login_skips_device_flow(self):
        runner = QueueRunner([completed({"identity": "user"})])
        messages = []

        result = LarkAuthManager("assistant-bot", runner=runner).ensure_user(
            "calendar", messages.append
        )

        self.assertEqual(result, AuthResult(True, ""))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(messages, [])

    def test_missing_login_sends_only_url_and_completes(self):
        runner = QueueRunner([
            completed({"identity": "bot", "note": "No user logged in"}),
            completed({
                "ok": True,
                "verification_uri_complete": "https://example.test/verify?code=public",
                "device_code": "private-device-code",
            }),
            completed({"ok": True}),
        ])
        messages = []

        result = LarkAuthManager("assistant-bot", runner=runner).ensure_user(
            "calendar", messages.append
        )

        self.assertTrue(result.ok)
        self.assertEqual(messages, [
            "此操作需要你的飞书用户授权，请点击链接完成授权：\n"
            "https://example.test/verify?code=public"
        ])
        self.assertNotIn("private-device-code", "".join(messages))
        for argv, _ in runner.calls:
            self.assertEqual(argv[:3], ["lark-cli", "--profile", "assistant-bot"])

    def test_invalid_secret_returns_stable_error_without_raw_output(self):
        runner = QueueRunner([
            completed({"identity": "bot"}),
            completed("secret details", returncode=1,
                      stderr="The client secret is invalid: private-value"),
        ])

        result = LarkAuthManager("assistant-bot", runner=runner).ensure_user(
            "calendar", lambda _: None
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "飞书授权启动失败，请联系管理员检查应用凭证。")
        self.assertNotIn("private-value", result.message)

    def test_concurrent_calls_share_one_device_flow(self):
        started = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        calls = []

        def runner(argv, **kwargs):
            with call_lock:
                calls.append(argv)
                index = len(calls)
            if index == 1:
                return completed({"identity": "bot"})
            if index == 2:
                started.set()
                release.wait(2)
                return completed({
                    "verification_uri": "https://example.test/verify",
                    "device_code": "private-code",
                })
            return completed({"ok": True})

        manager = LarkAuthManager("assistant-bot", runner=runner)
        results = []
        notifications = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    manager.ensure_user("calendar", notifications.append)
                )
            )
            for _ in range(2)
        ]
        threads[0].start()
        self.assertTrue(started.wait(1))
        threads[1].start()
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(2)

        login_starts = [argv for argv in calls if "--no-wait" in argv]
        self.assertEqual(len(login_starts), 1)
        self.assertEqual(results, [AuthResult(True, ""), AuthResult(True, "")])
        self.assertEqual(len(notifications), 1)


if __name__ == "__main__":
    unittest.main()
