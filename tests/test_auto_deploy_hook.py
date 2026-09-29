import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AutoDeployHookTests(unittest.TestCase):
    def test_post_receive_only_dispatches_master_and_uses_controlled_script(self):
        hook = (ROOT / "deploy" / "post-receive").read_text(encoding="utf-8")
        self.assertIn("refs/heads/master", hook)
        self.assertIn("assistant-auto-deploy", hook)
        self.assertNotIn("reset --hard", hook)

    def test_auto_deploy_requires_clean_worktree_and_fast_forward(self):
        script = (ROOT / "deploy" / "auto-deploy.sh").read_text(encoding="utf-8")
        self.assertIn("status --porcelain", script)
        self.assertIn("pull --ff-only", script)
        self.assertIn("verify-alinux3.sh\" --tests", script)
        self.assertIn("systemctl restart assistant.service", script)
        self.assertNotIn("git reset --hard", script)


if __name__ == "__main__":
    unittest.main()
