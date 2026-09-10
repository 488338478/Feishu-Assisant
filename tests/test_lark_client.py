import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu import client


class RunLarkCliTests(unittest.TestCase):
    def test_user_commands_set_identity_explicitly(self):
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "identity": "user"}), ""
        )

        with patch.object(client.subprocess, "run", return_value=completed) as run:
            client.run_lark_cli(["calendar", "+agenda"], as_bot=False)

        argv = run.call_args.args[0]
        self.assertIn("--as", argv)
        self.assertEqual(argv[argv.index("--as") + 1], "user")

    def test_does_not_inject_unsupported_format_flag(self):
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps({"success": True}), ""
        )

        with patch.object(client.subprocess, "run", return_value=completed) as run:
            result = client.run_lark_cli(
                [
                    "docs",
                    "+update",
                    "--doc",
                    "doc-token",
                    "--mode",
                    "append",
                    "--markdown",
                    "text",
                ]
            )

        argv = run.call_args.args[0]
        self.assertNotIn("--format", argv)
        self.assertEqual(result, {"success": True})

    def test_create_document_uses_current_content_flags(self):
        with patch.object(client, "run_lark_cli", return_value={"ok": True}) as run:
            result = client.create_document(
                "标题", "正文", wiki_space="wiki-parent-token"
            )

        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "docs",
                "+create",
                "--title",
                "标题",
                "--doc-format",
                "markdown",
                "--content",
                "正文",
                "--parent-token",
                "wiki-parent-token",
            ],
        )
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
