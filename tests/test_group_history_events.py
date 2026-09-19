import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant import handlers


class GroupHistoryEventTests(unittest.TestCase):
    def test_recall_removes_only_the_message_in_the_event_group(self):
        data = SimpleNamespace(event=SimpleNamespace(chat_id="g1", message_id="m1"))
        with patch.object(handlers.group_history_store, "remove", return_value=True) as remove:
            handlers.on_message_recalled(data)

        remove.assert_called_once_with("g1", "m1")


if __name__ == "__main__":
    unittest.main()
