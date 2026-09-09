import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProfilePolicyTests(unittest.TestCase):
    def test_docs_profile_uses_user_for_search_and_denies_auth_commands(self):
        instructions = (ROOT / "profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
        settings = json.loads(
            (ROOT / "profiles/docs/.claude/settings.json").read_text(encoding="utf-8")
        )

        self.assertIn("`docs +search` 只支持 `--as user`", instructions)
        self.assertNotIn('docs +search --query "关键词" --as bot', instructions)
        self.assertIn("[LARK_USER_AUTH_REQUIRED:", instructions)
        denies = settings["permissions"]["deny"]
        self.assertIn("Bash(lark-cli auth login:*)", denies)
        self.assertIn("Bash(lark-cli --profile * auth login:*)", denies)
        self.assertIn("Bash(lark-cli auth logout:*)", denies)
        self.assertIn("Bash(lark-cli --profile * auth logout:*)", denies)

    def test_docs_instruction_files_are_kept_in_sync(self):
        claude = (ROOT / "profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
        agents = (ROOT / "profiles/docs/AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(claude, agents)

    def test_profiles_do_not_claim_format_is_universal(self):
        for relative in (
            "profiles/docs/CLAUDE.md",
            "profiles/docs/AGENTS.md",
            "profiles/dev/CLAUDE.md",
            "profiles/thesis/CLAUDE.md",
        ):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("<命令> --format json", text)

    def test_active_profiles_use_canonical_project_name_with_legacy_alias(self):
        for relative in (
            "profiles/docs/CLAUDE.md",
            "profiles/docs/AGENTS.md",
            "profiles/dev/CLAUDE.md",
            "profiles/thesis/CLAUDE.md",
        ):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("《封卦手记》", text)
                self.assertIn("原名《卦阵手记》", text)

    def test_deployment_pins_validated_lark_cli_version(self):
        instructions = (ROOT / "deploy/README.md").read_text(encoding="utf-8")

        self.assertIn("@larksuite/cli@1.0.94", instructions)


if __name__ == "__main__":
    unittest.main()
