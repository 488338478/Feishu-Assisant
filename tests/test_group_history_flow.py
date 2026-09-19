import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant import handlers
from assistant.core.group_history import GroupHistoryConfig, HistoryResult


class GroupHistoryFlowTests(unittest.TestCase):
    def test_agent_can_request_history_and_answer_with_originals(self):
        stats = {"ok": True, "tools": 0, "duration": 0.1}
        runtime = MagicMock()
        runtime.run.side_effect = [
            (
                '[GROUP_HISTORY_SEARCH]{"query":"发布日期","time_range_hours":24,'
                '"sender_ids":[],"thread_id":"","limit":8}',
                stats,
            ),
            ("根据群聊原文，日期尚未最终确定。", stats),
        ]
        service = MagicMock()
        service.search.return_value = HistoryResult(
            "[09:32 张三] 发布日期先定在周五\n"
            "[09:35 李四] 周五资源不够，建议顺延到下周一",
            ("m1", "m2"), 40, False,
        )
        config = MagicMock()
        config.load.return_value = GroupHistoryConfig()

        with (
            patch.object(handlers, "runtime", runtime),
            patch.object(handlers, "group_history_service", service),
            patch.object(handlers, "group_history_config", config),
            patch.object(handlers.scheduler, "configure", return_value=None),
            patch.object(handlers.memory, "auto_learn"),
        ):
            result = handlers.process_message(
                "什么时候发布？", "g1", "u3", object(), chat_type="group",
                reply_to="m-parent", thread_id="",
            )

        self.assertEqual(result, "根据群聊原文，日期尚未最终确定。")
        service.search.assert_called_once()
        self.assertEqual(service.search.call_args.args[1].thread_id, "m-parent")
        self.assertEqual(
            runtime.run.call_args_list[1].kwargs["group_history_context"],
            service.search.return_value.text,
        )
        audit = runtime.audit_event.call_args.args[0]
        self.assertEqual(audit["event"], "group_history_search")
        self.assertEqual(audit["message_ids"], ["m1", "m2"])
        self.assertNotIn("发布日期先定在周五", str(audit))

    def test_private_chat_cannot_enter_group_history_retrieval(self):
        runtime = MagicMock()
        runtime.run.return_value = (
            '[GROUP_HISTORY_SEARCH]{"query":"秘密"}',
            {"ok": True, "tools": 0, "duration": 0.1},
        )
        service = MagicMock()
        with (
            patch.object(handlers, "runtime", runtime),
            patch.object(handlers, "group_history_service", service),
            patch.object(handlers.scheduler, "configure", return_value=None),
            patch.object(handlers.memory, "auto_learn"),
        ):
            result = handlers.process_message("查一下", "private", "u1", object())

        self.assertIn("对应群聊", result)
        service.search.assert_not_called()

    def test_search_rounds_never_exceed_hot_config_limit(self):
        marker = '[GROUP_HISTORY_SEARCH]{"query":"发布"}'
        runtime = MagicMock()
        stats = {"ok": True, "tools": 0, "duration": 0.1}
        runtime.run.side_effect = [
            (marker, stats), (marker, stats), (marker, stats),
            ("根据已检索到的原文，目前只能确定仍在讨论。", stats),
        ]
        service = MagicMock()
        service.search.return_value = HistoryResult(
            "[09:32 张三] 原文", ("m1",), 12, False
        )
        config = MagicMock()
        config.load.return_value = GroupHistoryConfig(max_search_rounds=2)
        with (
            patch.object(handlers, "runtime", runtime),
            patch.object(handlers, "group_history_service", service),
            patch.object(handlers, "group_history_config", config),
            patch.object(handlers.scheduler, "configure", return_value=None),
            patch.object(handlers.memory, "auto_learn"),
        ):
            result = handlers.process_message(
                "继续找", "g1", "u1", object(), chat_type="group"
            )

        self.assertEqual(service.search.call_count, 2)
        self.assertEqual(runtime.run.call_count, 4)
        self.assertIn("仍在讨论", result)
        self.assertTrue(runtime.run.call_args.kwargs["group_history_exhausted"])


if __name__ == "__main__":
    unittest.main()
