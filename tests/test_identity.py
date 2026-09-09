import sys
import types
import unittest
from pathlib import Path

assistant_package = types.ModuleType("assistant")
assistant_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", assistant_package)

from assistant.feishu.identity import required_user_domain


class IdentityTests(unittest.TestCase):
    def test_document_discovery_requires_user(self):
        cases = [
            "搜索项目周报",
            "搜索知识库",
            "找一下总策划文档并总结",
            "查找 GDD",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(required_user_domain(text), "docs")

    def test_personal_resources_require_user(self):
        cases = {
            "看看我明天的日程": "calendar",
            "我的未完成任务": "task",
            "查我昨天参加的会议": "vc",
            "整理我的会议纪要": "minutes",
            "看看我的邮件": "mail",
            "我的考勤记录": "attendance",
            "查看我的个人信息": "contact",
        }
        for text, domain in cases.items():
            with self.subTest(text=text):
                self.assertEqual(required_user_domain(text), domain)

    def test_shared_resources_stay_bot_first(self):
        for text in ["读取项目周报", "列出群聊", "创建一篇文档"]:
            with self.subTest(text=text):
                self.assertIsNone(required_user_domain(text))

    def test_domain_word_without_personal_marker_stays_bot_first(self):
        for text in ["查看团队日程", "整理项目会议纪要", "搜索任务清单"]:
            with self.subTest(text=text):
                self.assertIsNone(required_user_domain(text))


if __name__ == "__main__":
    unittest.main()
