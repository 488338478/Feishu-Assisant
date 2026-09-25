import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.agent import runtime


class RuntimeProfileResolutionTests(unittest.TestCase):
    def test_user_profile_overrides_chat_profile(self):
        config = {
            "default_profile": "docs",
            "chat_profiles": {"docs-group": "docs"},
            "user_profiles": {"developer": "dev"},
            "default_tier": "read",
            "user_tiers": {},
        }
        with patch.object(runtime, "_load_runtime_config", return_value=config):
            self.assertEqual(runtime.resolve_profile("docs-group", "developer"), "dev")

    def test_chat_profile_is_used_without_user_override(self):
        config = {
            "default_profile": "docs",
            "chat_profiles": {"dev-group": "dev"},
            "user_profiles": {},
            "default_tier": "read",
            "user_tiers": {},
        }
        with patch.object(runtime, "_load_runtime_config", return_value=config):
            self.assertEqual(runtime.resolve_profile("dev-group", "reader"), "dev")


if __name__ == "__main__":
    unittest.main()
