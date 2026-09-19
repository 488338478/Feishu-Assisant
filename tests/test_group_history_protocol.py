import sys
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.group_history_protocol import parse_history_request


class GroupHistoryProtocolTests(unittest.TestCase):
    def test_parses_search_request_only_when_entire_response_is_marker(self):
        text = (
            '[GROUP_HISTORY_SEARCH]{"query":"发布日期","time_range_hours":24,'
            '"sender_ids":[],"thread_id":"","limit":8}'
        )

        request = parse_history_request(text)

        self.assertEqual(request.kind, "search")
        self.assertEqual(request.query, "发布日期")

    def test_rejects_model_controlled_chat_id(self):
        text = '[GROUP_HISTORY_SEARCH]{"chat_id":"g2","query":"秘密"}'
        self.assertIsNone(parse_history_request(text))


if __name__ == "__main__":
    unittest.main()
