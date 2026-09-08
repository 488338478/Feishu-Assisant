import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.client import fetch_bot_open_id


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return self.response


class BotIdentityTests(unittest.TestCase):
    def test_fetches_open_id_from_bot_info_endpoint(self):
        payload = {"code": 0, "bot": {"open_id": "bot-open-id"}}
        response = SimpleNamespace(
            success=lambda: True,
            raw=SimpleNamespace(content=json.dumps(payload).encode("utf-8")),
        )
        client = FakeClient(response)

        result = fetch_bot_open_id(client)

        self.assertEqual(result, "bot-open-id")
        self.assertEqual(client.requests[0].uri, "/open-apis/bot/v3/info")

    def test_failure_returns_empty_identity_for_fail_closed_gate(self):
        response = SimpleNamespace(success=lambda: False, raw=None)
        self.assertEqual(fetch_bot_open_id(FakeClient(response)), "")


if __name__ == "__main__":
    unittest.main()
