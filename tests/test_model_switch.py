import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant import handlers
from assistant.agent import runtime


class ModelSwitchHandlerTests(unittest.TestCase):
    def setUp(self):
        self.client = object()

    def test_explicit_model_switch_is_deterministic_and_scoped(self):
        with patch.object(handlers.scheduler, "configure", return_value=None), \
             patch.object(handlers.runtime, "set_model", return_value="deepseek-v4-pro") as set_model, \
             patch.object(handlers.runtime, "run") as run:
            result = handlers.process_message(
                "切换模型 DeepSeek 4.0 Pro", "group-1", "user-1", self.client,
                chat_type="group",
            )

        self.assertIn("DeepSeek 4.0 Pro", result)
        set_model.assert_called_once_with("group-1", "deepseek-v4-pro", "group")
        run.assert_not_called()

    def test_current_model_query_is_deterministic(self):
        with patch.object(handlers.scheduler, "configure", return_value=None), \
             patch.object(handlers.runtime, "describe_model", return_value="当前模型：DeepSeek 4.1"), \
             patch.object(handlers.runtime, "run") as run:
            result = handlers.process_message("当前模型", "chat", "user", self.client)

        self.assertEqual(result, "当前模型：DeepSeek 4.1")
        run.assert_not_called()

    def test_model_switch_accepts_colon_and_spaces(self):
        with patch.object(handlers.scheduler, "configure", return_value=None), \
             patch.object(handlers.runtime, "set_model", return_value="deepseek-v4-flash") as set_model, \
             patch.object(handlers.runtime, "run") as run:
            result = handlers.process_message(
                "切换模型： DeepSeek 4.1", "chat", "user", self.client
            )

        self.assertIn("DeepSeek 4.1", result)
        set_model.assert_called_once_with("chat", "deepseek-v4-flash", "p2p")
        run.assert_not_called()

    def test_fuzzy_model_intent_guides_without_switching(self):
        with patch.object(handlers.scheduler, "configure", return_value=None), \
             patch.object(handlers.runtime, "set_model") as set_model, \
             patch.object(handlers.runtime, "run") as run:
            result = handlers.process_message("我想换个模型", "chat", "user", self.client)

        self.assertIn("切换模型 DeepSeek 4.1", result)
        self.assertIn("切换模型 DeepSeek 4.0 Pro", result)
        set_model.assert_not_called()
        run.assert_not_called()

    def test_unknown_model_reports_allowed_choices(self):
        with patch.object(handlers.scheduler, "configure", return_value=None), \
             patch.object(handlers.runtime, "run") as run:
            result = handlers.process_message(
                "切换模型 GPT-5", "chat", "user", self.client
            )

        self.assertIn("只支持", result)
        self.assertIn("DeepSeek 4.0 Pro", result)
        run.assert_not_called()


class ModelPreferenceTests(unittest.TestCase):
    def test_model_preference_is_persisted_by_chat_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_preferences.json"
            with patch.object(runtime, "MODEL_PREFERENCES_FILE", path):
                self.assertEqual(runtime.resolve_model("g", "group"), runtime.DEFAULT_MODEL)
                runtime.set_model("g", "deepseek-v4-pro", "group")
                self.assertEqual(runtime.resolve_model("g", "group"), "deepseek-v4-pro")
                self.assertEqual(runtime.resolve_model("g", "p2p"), runtime.DEFAULT_MODEL)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["group:g"], "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
