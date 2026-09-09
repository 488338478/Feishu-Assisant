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


if __name__ == "__main__":
    unittest.main()
