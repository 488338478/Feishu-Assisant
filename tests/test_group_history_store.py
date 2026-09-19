import tempfile
import sys
import types
import unittest
import json
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.core.group_history import (
    GroupHistoryConfigLoader,
    GroupHistoryStore,
    GroupMessage,
)


class GroupHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "history.sqlite3"

    def test_store_preserves_content_and_filters_by_current_group(self):
        store = GroupHistoryStore(self.db)
        original = "第一行  \n第二行，不要改。"
        store.record(GroupMessage("m1", "g1", "u1", "张三", 1000, "", "", "text", original))
        store.record(GroupMessage("m2", "g2", "u2", "李四", 2000, "", "", "text", "另一个群"))

        self.assertEqual(store.get("g1", "m1").content, original)
        self.assertEqual([message.message_id for message in store.recent("g1", limit=10)], ["m1"])
        self.assertIsNone(store.get("g1", "m2"))

    def test_invalid_hot_config_keeps_last_valid_values(self):
        config_path = Path(self.tmp.name) / "group_history_config.json"
        config_path.write_text(json.dumps({"natural_soft_chars": 3000}), encoding="utf-8")
        loader = GroupHistoryConfigLoader(config_path)
        first = loader.load()

        config_path.write_text(json.dumps({"natural_soft_chars": -1}), encoding="utf-8")

        self.assertEqual(loader.load(), first)

    def test_budget_smaller_than_a_source_slice_is_rejected(self):
        config_path = Path(self.tmp.name) / "group_history_config.json"
        config_path.write_text(json.dumps({"retrieval_token_budget": 6000}), encoding="utf-8")
        loader = GroupHistoryConfigLoader(config_path)
        first = loader.load()

        config_path.write_text(json.dumps({"retrieval_token_budget": 1000}), encoding="utf-8")

        self.assertEqual(loader.load(), first)


if __name__ == "__main__":
    unittest.main()
