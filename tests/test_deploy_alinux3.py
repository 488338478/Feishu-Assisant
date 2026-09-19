import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AlibabaLinuxDeploymentAssetsTests(unittest.TestCase):
    def test_installer_targets_alibaba_linux_without_installing_git(self):
        text = (ROOT / "deploy" / "install-alinux3.sh").read_text(encoding="utf-8")
        self.assertIn('== "alinux"', text)
        self.assertIn("dnf install", text)
        self.assertNotIn("dnf install git", text)
        self.assertIn("command -v git", text)
        self.assertIn("@larksuite/cli@1.0.94", text)
        self.assertIn("@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION", text)
        self.assertIn('runuser -u "$AGENT_USER"', text)

    def test_service_keeps_cli_state_in_writable_agent_directory(self):
        text = (ROOT / "deploy" / "assistant.service").read_text(encoding="utf-8")
        self.assertIn("Environment=HOME=/srv/agent/home", text)
        self.assertIn("ReadWritePaths=/srv/agent/data /srv/agent/home", text)
        self.assertIn("/srv/agent/npm/bin", text)
        self.assertIn("ExecStart=/srv/agent/venv/bin/python -m assistant.main", text)

    def test_verifier_never_prints_secret_values(self):
        text = (ROOT / "deploy" / "verify-alinux3.sh").read_text(encoding="utf-8")
        self.assertNotIn("cat \"$ENV_FILE\"", text)
        self.assertIn("FEISHU_APP_SECRET", text)
        self.assertIn("systemctl is-active", text)
        self.assertIn("-m unittest discover", text)
        self.assertIn("TEST_DATA=$(mktemp -d)", text)
        self.assertIn('ASSISTANT_DATA_DIR="$TEST_DATA"', text)

    def test_ai_runbook_has_install_upgrade_backup_and_rollback(self):
        text = (ROOT / "deploy" / "AI_DEPLOY_ALINUX3.md").read_text(encoding="utf-8")
        for heading in ("首次部署", "升级", "备份", "回滚", "验收"):
            self.assertIn(heading, text)
        self.assertIn("/srv/agent/assistant", text)
        self.assertIn("im:message.group_msg", text)
        self.assertIn("calendar:calendar.event:read", text)
        self.assertIn("git clone", text)
        self.assertIn("SERVER_DEPLOY.md", text)

    def test_root_bootstrap_starts_from_repository_url(self):
        text = (ROOT / "SERVER_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("REPO_URL", text)
        self.assertIn("git clone", text)
        self.assertIn("deploy/AI_DEPLOY_ALINUX3.md", text)
        self.assertIn("deploy/install-alinux3.sh", text)

    def test_backup_pauses_and_restores_a_running_service(self):
        text = (ROOT / "deploy" / "backup-alinux3.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl stop assistant.service", text)
        self.assertIn("trap restore_service EXIT", text)
        self.assertIn("systemctl start assistant.service", text)


if __name__ == "__main__":
    unittest.main()
