import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProfilePolicyTests(unittest.TestCase):
    def test_docs_profile_is_bot_first_and_denies_auth_commands(self):
        instructions = (ROOT / "profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
        settings = json.loads(
            (ROOT / "profiles/docs/.claude/settings.json").read_text(encoding="utf-8")
        )

        self.assertIn("默认使用 `--as bot`", instructions)
        self.assertIn("[LARK_USER_AUTH_REQUIRED:", instructions)
        denies = settings["permissions"]["deny"]
        self.assertIn("Bash(lark-cli auth login:*)", denies)
        self.assertIn("Bash(lark-cli --profile * auth login:*)", denies)
        self.assertIn("Bash(lark-cli auth logout:*)", denies)
        self.assertIn("Bash(lark-cli --profile * auth logout:*)", denies)


if __name__ == "__main__":
    unittest.main()
