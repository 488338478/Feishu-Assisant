import sys
import tempfile
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.core.group_history import (
    GroupHistoryConfig,
    GroupHistoryService,
    GroupHistoryStore,
    GroupMessage,
    HistoryQuery,
    MessageNotFound,
)


class StaticConfig:
    def __init__(self, value):
        self.value = value

    def load(self):
        return self.value


class GroupHistoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GroupHistoryStore(Path(self.tmp.name) / "history.sqlite3")
        self.service = GroupHistoryService(
            self.store, StaticConfig(GroupHistoryConfig())
        )

    def test_search_formats_metadata_without_changing_content(self):
        original = "发布日期先定在周五  \n测试不过就顺延。"
        self.store.record(GroupMessage(
            "m1", "g1", "u1", "张三", 1757640720000,
            "", "", "text", original,
        ))

        result = self.service.search(
            "g1", HistoryQuery(query="发布日期"), now_ms=1757644320000
        )

        self.assertEqual(result.text, "[09:32 张三] " + original)
        self.assertEqual(result.message_ids, ("m1",))

    def test_long_log_is_exactly_sliced_and_can_be_expanded(self):
        original = "\n".join(
            f"2026-09-12 09:32:00 ERROR item={index}" for index in range(500)
        )
        self.store.record(GroupMessage(
            "log", "g1", "bot", "构建机器人", 1757640720000,
            "", "", "text", original,
        ))

        first = self.service.search(
            "g1", HistoryQuery(query="ERROR"), now_ms=1757644320000
        )
        second = self.service.expand("g1", "log", 2)

        self.assertIn("原文片段 1/", first.text)
        self.assertIn("message_id=log", first.text)
        self.assertIn("原文片段 2/", second.text)
        self.assertTrue(original.startswith(first.text.split("] ", 1)[1]))

    def test_long_natural_message_stays_complete_when_budget_allows(self):
        original = "这是一段人工输入的项目说明，包含完整观点和原因。" * 180
        self.store.record(GroupMessage(
            "natural", "g1", "u1", "张三", 1757640720000,
            "", "", "text", original,
        ))

        result = self.service.search(
            "g1", HistoryQuery(query="项目说明"), now_ms=1757644320000
        )

        self.assertIn(original, result.text)
        self.assertNotIn("原文片段", result.text)

    def test_expand_cannot_read_a_message_from_another_group(self):
        self.store.record(GroupMessage(
            "m2", "g2", "u2", "李四", 1757640720000,
            "", "", "text", "另一个群的秘密内容" * 300,
        ))

        with self.assertRaises(MessageNotFound):
            self.service.expand("g1", "m2", 1)

    def test_search_excludes_the_current_trigger_message(self):
        self.store.record(GroupMessage(
            "trigger", "g1", "u1", "提问人", 1757640720000,
            "", "", "text", "什么时候发布？",
        ))
        self.store.record(GroupMessage(
            "answer", "g1", "u2", "张三", 1757640660000,
            "", "", "text", "项目周五发布。",
        ))

        result = self.service.search(
            "g1",
            HistoryQuery(query="发布", exclude_message_ids=("trigger",)),
            now_ms=1757644320000,
        )

        self.assertNotIn("什么时候发布", result.text)
        self.assertEqual(result.message_ids, ("answer",))

    def test_selected_messages_are_rendered_in_conversation_order(self):
        base = 1757640720000
        rows = [
            ("m1", "张三", "发布日期先定在周五", base),
            ("m2", "李四", "周五资源不够，建议顺延到下周一", base + 180000),
            ("m3", "王五", "等测试结果出来再决定", base + 540000),
        ]
        for message_id, name, content, sent_at in rows:
            self.store.record(GroupMessage(
                message_id, "g1", message_id, name, sent_at,
                "", "", "text", content,
            ))

        result = self.service.search(
            "g1", HistoryQuery(query="", limit=3), now_ms=base + 600000
        )

        self.assertEqual(
            result.text,
            "[09:32 张三] 发布日期先定在周五\n"
            "[09:35 李四] 周五资源不够，建议顺延到下周一\n"
            "[09:41 王五] 等测试结果出来再决定",
        )

    def test_thread_search_includes_the_root_message_and_replies(self):
        base = 1757640720000
        self.store.record(GroupMessage(
            "root", "g1", "u1", "张三", base,
            "", "", "text", "初始方案是周五发布",
        ))
        self.store.record(GroupMessage(
            "reply", "g1", "u2", "李四", base + 60000,
            "root", "root", "text", "资源不足，建议下周一",
        ))

        result = self.service.search(
            "g1", HistoryQuery(thread_id="root", limit=8), now_ms=base + 120000
        )

        self.assertEqual(result.message_ids, ("root", "reply"))


if __name__ == "__main__":
    unittest.main()
