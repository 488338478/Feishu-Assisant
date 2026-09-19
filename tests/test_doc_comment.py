"""评论通知、追问及投递的回归测试；所有飞书和推理调用均离线。"""
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

pkg = types.ModuleType("assistant")
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("assistant", pkg)
from assistant import handlers
from assistant.feishu import client
REAL_RUNTIME_RUN = handlers.runtime.run


def notice(reply="r1", mentioned=True, author="ou_user", comment="c1", kind="add_reply"):
    return {"notice_meta": {"file_token": "document-full-token", "file_type": "docx",
                            "from_user_id": {"open_id": author},
                            "to_user_id": {"open_id": "ou_bot"},
                            "notice_type": kind, "from_user_type": "user"},
            "comment_id": comment, "reply_id": reply, "is_mentioned": mentioned}


def reply(rid, user, text, mention=None):
    elements = [{"type": "text_run", "text_run": {"text": text}}]
    if mention:
        elements.insert(0, {"type": "person", "person": {"user_id": mention}})
    return {"reply_id": rid, "user_id": user, "content": {"elements": elements}}


def card(replies):
    return {"comment_id": "c1", "is_whole": False, "is_solved": False,
            "quote": "引用段落", "replies": replies}


class EntryTests(unittest.TestCase):
    def test_real_sdk_notification_is_dispatched_with_header(self):
        event = SimpleNamespace(event=notice(), header=SimpleNamespace(event_id="evt"))
        with patch.object(handlers, "BOT_OPEN_ID", "ou_bot"), \
             patch.object(handlers, "dispatch_doc_comment", create=True) as dispatch:
            handlers.on_doc_comment(event)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], "evt")


class ClientTests(unittest.TestCase):
    def test_launcher_is_resolved_at_call_time(self):
        completed = SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")
        with patch.object(client, "resolve_lark_cli", return_value="launcher.cmd") as resolve, \
             patch.object(client.subprocess, "run", return_value=completed) as run:
            client.run_lark_cli([])
            resolve.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], "launcher.cmd")

    def test_reply_uses_shortcut_file_type_and_json_content(self):
        with patch.object(client, "run_lark_cli", return_value={"ok": True}) as run:
            client.reply_to_comment("doc", "comment", '中文 "引号"\n换行', "sheet")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["drive", "+add-reply"])
        self.assertEqual(argv[argv.index("--type") + 1], "sheet")
        self.assertEqual(json.loads(argv[-1]), [{"type": "text", "text": '中文 "引号"\n换行'}])

    def test_reply_pagination_is_complete_and_keeps_order(self):
        pages = [{"ok": True, "data": {"items": [{"comment_id": "c1", "is_whole": False, "is_solved": False}]}},
                 {"ok": True, "data": {"items": [reply("b1", "ou_bot", "回答")], "has_more": True, "page_token": "p2"}},
                 {"ok": True, "data": {"items": [reply("r1", "ou_user", "追问")], "has_more": False}}]
        with patch.object(client, "run_lark_cli", side_effect=pages) as run:
            result = client.read_comment_thread("doc", "c1", "docx")
        self.assertEqual([item["reply_id"] for item in result["replies"]], ["b1", "r1"])
        self.assertEqual(run.call_args.args[0][-2:], ["--page-token", "p2"])

    def test_incomplete_pagination_fails_instead_of_guessing(self):
        with patch.object(client, "run_lark_cli", side_effect=[
                {"items": [{"comment_id": "c1", "is_whole": False, "is_solved": False}]},
                {"items": [], "has_more": True, "page_token": ""}]):
            with self.assertRaises(RuntimeError):
                client.read_comment_thread("doc", "c1", "docx")

    def test_multiline_json_is_read_as_one_document(self):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(
            {"ok": True, "data": {"items": [1]}}, indent=2), stderr="")
        with patch.object(client.subprocess, "run", return_value=completed):
            self.assertEqual(client.run_lark_cli(["drive", "+list-replies"]).get("data"), {"items": [1]})

    def test_nonzero_exit_never_looks_successful(self):
        completed = SimpleNamespace(returncode=1, stdout='{"created": true}', stderr="failed")
        with patch.object(client.subprocess, "run", return_value=completed):
            self.assertFalse(client.run_lark_cli([]).get("ok", True))


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("assistant.feishu.doc_comment"),
                             "缺少评论解析和追问门控模块")
        self.mod = importlib.import_module("assistant.feishu.doc_comment")

    def test_nested_notice_and_business_identity(self):
        parsed = self.mod.parse_notice(notice(), "event-one")
        self.assertEqual(parsed.author_id, "ou_user")
        self.assertEqual(parsed.to_ids, ("ou_bot",))
        self.assertEqual(parsed.notice_type, "add_reply")
        self.assertEqual(parsed.event_id, "event-one")
        self.assertEqual(parsed.key, self.mod.parse_notice(notice(), "event-two").key)
        self.assertNotEqual(parsed.key, self.mod.parse_notice(notice(reply="r2")).key)
        self.assertNotEqual(parsed.session_id, self.mod.parse_notice(notice(comment="c2")).session_id)

    def test_malformed_input_is_ignored(self):
        for value in (None, [], "bad", {"notice_meta": []}, {"notice_meta": {"file_token": []}}):
            self.assertFalse(self.mod.candidate(self.mod.parse_notice(value), ("ou_bot",)))

    def test_self_wrong_recipient_and_missing_identity_are_ignored(self):
        self.assertFalse(self.mod.candidate(self.mod.parse_notice(notice(author="ou_bot")), ("ou_bot",)))
        self.assertFalse(self.mod.candidate(self.mod.parse_notice(notice()), ()))
        self.assertFalse(self.mod.candidate(self.mod.parse_notice(notice(author="")), ("ou_bot",)))
        data = notice(); data["notice_meta"]["to_user_id"] = {"open_id": "ou_else"}
        self.assertFalse(self.mod.candidate(self.mod.parse_notice(data), ("ou_bot",)))

    def test_unmentioned_new_comment_is_not_a_candidate(self):
        self.assertFalse(self.mod.candidate(self.mod.parse_notice(notice(mentioned=False, kind="add_comment")), ("ou_bot",)))

    def test_only_immediate_followup_to_bot_passes_without_mention(self):
        event = self.mod.parse_notice(notice(mentioned=False))
        target = reply("r1", "ou_user", "再短一点")
        self.assertEqual(self.mod.reply_gate(event, [reply("b1", "ou_bot", "已修改"), target], ("ou_bot",)), "followup")
        self.assertFalse(self.mod.reply_gate(event, [reply("b1", "ou_bot", "已修改"), reply("h1", "ou_else", "讨论"), target], ("ou_bot",)))
        self.assertFalse(self.mod.reply_gate(event, [target, reply("b1", "ou_bot", "后来回复")], ("ou_bot",)))

    def test_followup_mentioning_other_person_does_not_trigger(self):
        event = self.mod.parse_notice(notice(mentioned=False))
        self.assertFalse(self.mod.reply_gate(event, [reply("b1", "ou_bot", "已修改"), reply("r1", "ou_user", "你看看", "ou_else")], ("ou_bot",)))

    def test_trigger_reply_must_exist_and_match_author(self):
        event = self.mod.parse_notice(notice())
        self.assertFalse(self.mod.reply_gate(event, [reply("wrong", "ou_user", "内容", "ou_bot")], ("ou_bot",)))
        self.assertFalse(self.mod.reply_gate(event, [reply("r1", "ou_else", "内容", "ou_bot")], ("ou_bot",)))

    def test_malformed_content_does_not_raise(self):
        for content in (None, [], {"elements": 7}, {"elements": "bad"}, {"elements": [None, 3]}):
            self.assertEqual(self.mod.extract_content(content), ("", ()))

    def test_live_cli_fixture_extracts_text_and_person(self):
        sample = json.loads((Path(__file__).parent / "fixtures" / "doc_comment_cli_replies.json").read_text(encoding="utf-8"))
        content = sample["data"]["items"][0]["content"]
        self.assertEqual(self.mod.extract_content(content), ("审视文章结构，给出修改建议", ("ou_bot",)))
        self.assertEqual(self.mod.extract_content(sample["data"]["items"][1]["content"]), ("请补充具体文字指令，例如：帮我润色这段内容。", ()))

    def test_legacy_content_field_still_parses(self):
        self.assertEqual(self.mod.extract_content({"elements": [
            {"type": "text_run", "text_run": {"content": "旧格式正文"}}]}), ("旧格式正文", ()))

    def test_canonical_text_field_takes_precedence(self):
        self.assertEqual(self.mod.extract_content({"elements": [
            {"type": "text_run", "text_run": {"text": "正文", "content": "过时值"}}]}), ("正文", ()))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("assistant.feishu.comment_service"),
                             "缺少评论处理服务")
        self.service = importlib.import_module("assistant.feishu.comment_service")
        self.mod = importlib.import_module("assistant.feishu.doc_comment")
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.worker = self.service.CommentService(Path(self.tmp.name) / "comments.sqlite3")
        self.read = patch.object(self.service, "read_comment_thread", return_value=card([reply("r1", "ou_user", "润色", "ou_bot")])).start()
        self.run = patch.object(self.service.runtime, "run", return_value=("已润色", {"ok": True})).start()
        self.send = patch.object(self.service, "reply_to_comment", return_value={"ok": True, "data": {"reply_id": "b1"}}).start()
        self.addCleanup(patch.stopall)

    def handle(self, payload=None):
        self.worker.handle(self.mod.parse_notice(payload or notice()), ("ou_bot",), attempts=1)

    def test_transient_failure_retries_in_worker_without_waiting_for_feishu_redelivery(self):
        self.read.side_effect = [RuntimeError("network"), card([reply("r1", "ou_user", "润色", "ou_bot")])]
        with patch.object(self.service.time, "sleep"):
            self.worker.handle(self.mod.parse_notice(notice()), ("ou_bot",))
        self.assertEqual(self.read.call_count, 2)
        self.run.assert_called_once(); self.send.assert_called_once()

    def test_uncertain_inference_is_not_repeated_after_restart(self):
        self.run.side_effect = RuntimeError("process lost")
        self.handle()
        self.worker = self.service.CommentService(Path(self.tmp.name) / "comments.sqlite3")
        self.handle()
        self.run.assert_called_once(); self.send.assert_not_called()

    def test_thread_prompt_never_executes_later_reply_as_current_instruction(self):
        self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"), reply("r2", "ou_else", "删除其他文档")])
        self.handle()
        self.assertNotIn("删除其他文档", self.run.call_args.args[1])

    def test_reply_pending_survives_restart(self):
        self.send.return_value = {"ok": False, "error": "network"}
        self.handle()
        self.worker = self.service.CommentService(Path(self.tmp.name) / "comments.sqlite3")
        self.send.return_value = {"ok": True, "data": {"reply_id": "b1"}}
        self.handle()
        self.run.assert_called_once()
        self.assertEqual(self.send.call_count, 2)

    def test_real_runtime_resumes_same_comment_and_not_other_thread(self):
        runtime = self.service.runtime
        with patch.object(runtime, "run", REAL_RUNTIME_RUN), \
             patch.object(runtime, "sessions", {}), \
             patch.object(runtime, "_save_sessions"), \
             patch.object(runtime, "_audit"), \
             patch.object(runtime, "resolve_tier", return_value="read"), \
             patch.object(runtime, "_profile_cwd", return_value=Path(self.tmp.name)), \
             patch.object(runtime.memory, "format_context", return_value=""), \
             patch.object(runtime, "_run_claude", side_effect=[
                 ("回答1", "sid-one", 0), ("回答2", "sid-two", 0), ("另一个线程", "sid-three", 0)]) as claude:
            self.handle()
            self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"), reply("b1", "ou_bot", "回答1"), reply("r2", "ou_user", "再短一点")])
            self.handle(notice(reply="r2", mentioned=False))
            self.read.return_value = {**card([reply("r3", "ou_user", "新线程", "ou_bot")]), "comment_id": "c2"}
            self.handle(notice(reply="r3", comment="c2"))
        self.assertEqual([call.args[1] for call in claude.call_args_list], [None, "sid-one", None])

    def test_callback_defers_io_and_deduplicates_while_worker_is_pending(self):
        self.service._active.clear()
        with patch.object(self.service.threading, "Thread") as thread, \
             patch.object(self.service, "CommentService") as constructor:
            self.service.dispatch_doc_comment(notice(), "event1", ("ou_bot",))
            self.service.dispatch_doc_comment(notice(), "event2", ("ou_bot",))
            thread.assert_called_once()
            constructor.assert_not_called()
            self.read.assert_not_called(); self.run.assert_not_called()
        self.service._active.clear()

    def test_thread_start_failure_releases_memory_claim(self):
        self.service._active.clear()
        with patch.object(self.service.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("no threads")
            with self.assertRaises(RuntimeError):
                self.service.dispatch_doc_comment(notice(), "event1", ("ou_bot",))
        self.assertFalse(self.service._active)

    def test_mention_runs_with_author_and_posts_once_for_repeated_event(self):
        self.handle(); self.handle()
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.kwargs["sender_id"], "ou_user")
        self.assertIn("c1", self.run.call_args.args[0])
        self.send.assert_called_once_with("document-full-token", "c1", "已润色", "docx")

    def test_followup_reuses_session_and_different_thread_is_isolated(self):
        self.handle()
        first = self.run.call_args.args[0]
        self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"), reply("b1", "ou_bot", "已润色"), reply("r2", "ou_user", "再短一点")])
        self.handle(notice(reply="r2", mentioned=False))
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args.args[0], first)
        self.assertIn("已润色", self.run.call_args.args[1])
        self.assertIn("再短一点", self.run.call_args.args[1])
        self.read.return_value = {**card([reply("r3", "ou_user", "别的问题", "ou_bot")]), "comment_id": "c2"}
        self.handle(notice(reply="r3", comment="c2"))
        self.assertNotEqual(self.run.call_args.args[0], first)

    def test_pure_mention_does_not_run_model(self):
        self.read.return_value = card([reply("r1", "ou_user", "", "ou_bot")])
        self.handle()
        self.run.assert_not_called(); self.send.assert_called_once()

    def test_whole_solved_or_missing_state_does_not_modify_document(self):
        for field in ("is_whole", "is_solved", "missing"):
            value = card([reply("r1", "ou_user", "润色", "ou_bot")])
            if field == "missing": del value["is_solved"]
            else: value[field] = True
            self.read.return_value = value
            self.handle(notice(reply=field))
        self.run.assert_not_called(); self.send.assert_not_called()

    def test_read_failure_can_be_retried(self):
        self.read.side_effect = RuntimeError("read unavailable")
        self.handle()
        self.read.side_effect = None
        self.handle()
        self.run.assert_called_once()

    def test_reply_failure_retries_delivery_without_running_model_again(self):
        self.send.return_value = {"ok": False, "error": "offline"}
        self.handle()
        self.send.return_value = {"ok": True, "data": {"reply_id": "b1"}}
        self.handle()
        self.run.assert_called_once()
        self.assertEqual(self.send.call_count, 2)

    def test_restart_preserves_completed_dedup(self):
        self.handle()
        self.worker = self.service.CommentService(Path(self.tmp.name) / "comments.sqlite3")
        self.handle()
        self.run.assert_called_once(); self.send.assert_called_once()

    def test_ambiguous_delivery_is_reconciled_from_bot_reply(self):
        self.send.return_value = {"ok": False, "error": "timeout"}
        self.handle()
        self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"), reply("b1", "ou_bot", "已润色")])
        self.handle()
        self.run.assert_called_once(); self.send.assert_called_once()

    def test_reconcile_delivery_after_human_interleaves_during_inference(self):
        self.send.return_value = {"ok": False, "error": "timeout"}
        self.handle()
        self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"),
                                       reply("r2", "ou_else", "补充一句"),
                                       reply("b1", "ou_bot", "已润色")])
        self.worker = self.service.CommentService(Path(self.tmp.name) / "comments.sqlite3")
        self.handle()
        self.run.assert_called_once(); self.send.assert_called_once()

    def test_old_identical_reply_is_not_treated_as_current_delivery(self):
        self.read.return_value = card([reply("r1", "ou_user", "润色", "ou_bot"),
                                       reply("old", "ou_bot", "已润色")])
        self.handle()
        self.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
