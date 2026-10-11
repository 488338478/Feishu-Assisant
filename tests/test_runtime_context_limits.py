import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

package = types.ModuleType('assistant')
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', package)
from assistant.agent import runtime


class ContextLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
            ('SESSIONS_FILE', Path(self.tmp.name) / 'sessions.json'),
            ('CONTEXT_COMPACTION_THRESHOLD_CHARS', 120000),
        ):
            self.stack.enter_context(patch.object(runtime, name, value))
        for name, value in (
            ('_profile_cwd', Path(self.tmp.name)),
            ('resolve_profile', 'docs'),
            ('resolve_tier', 'read'),
            ('resolve_model', runtime.DEFAULT_MODEL),
            ('_audit', None),
        ):
            self.stack.enter_context(patch.object(runtime, name, return_value=value))
        self.stack.enter_context(patch.object(runtime.memory, 'format_context', return_value=''))
        runtime.sessions.clear()
        runtime._chat_locks.clear()
        self.scope = runtime.conversation_key('chat', 'user', 'p2p', 'docs')

    def process(self, events, returncode=0, stderr=''):
        proc = Mock(returncode=returncode)
        proc.stdout = io.StringIO(''.join(json.dumps(evt) + '\n' for evt in events))
        proc.stderr = io.StringIO(stderr)
        return proc

    def test_structured_failure_is_raised_even_when_stdout_has_result_text(self):
        proc = self.process([
            {'type': 'result', 'is_error': True, 'result': 'Prompt is too long', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'Prompt is too long'):
                runtime._run_claude('继续', 'sid', Path(self.tmp.name), [], 5)

    def test_manual_compaction_accepts_boundary_without_normal_reply(self):
        for with_result in (False, True):
            with self.subTest(with_result=with_result):
                events = [
                    {'type': 'system', 'subtype': 'init', 'session_id': 'sid'},
                    {'type': 'system', 'subtype': 'compact_boundary', 'session_id': 'sid',
                     'compact_metadata': {'trigger': 'manual', 'pre_tokens': 179563}},
                ]
                if with_result:
                    events.append({'type': 'result', 'subtype': 'success', 'is_error': False,
                                   'result': '', 'session_id': 'sid'})
                proc = self.process(events)
                with patch.object(runtime.subprocess, 'Popen', return_value=proc):
                    result = runtime._compact_session('sid', Path(self.tmp.name), [], 5,
                                                       runtime.DEFAULT_MODEL)
                self.assertEqual(result, ('sid', 0))

    def test_empty_compaction_without_boundary_is_still_a_failure(self):
        proc = self.process([
            {'type': 'system', 'subtype': 'init', 'session_id': 'sid'},
            {'type': 'result', 'subtype': 'success', 'result': '', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'empty response'):
                runtime._compact_session('sid', Path(self.tmp.name), [], 5, runtime.DEFAULT_MODEL)

    def test_compact_boundary_does_not_hide_a_later_failure(self):
        for structured_failure, stderr, expected_error in (
            (False, 'gateway failed', 'gateway failed'),
            (True, 'gateway failed', 'gateway failed'),
            (False, '', 'exit code 1'),
        ):
            with self.subTest(structured_failure=structured_failure, stderr=stderr):
                events = [{'type': 'system', 'subtype': 'compact_boundary', 'session_id': 'sid'}]
                if structured_failure:
                    events.append({'type': 'result', 'is_error': True, 'errors': ['gateway failed']})
                proc = self.process(events, returncode=0 if structured_failure else 1,
                                    stderr=stderr)
                with patch.object(runtime.subprocess, 'Popen', return_value=proc):
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        runtime._compact_session('sid', Path(self.tmp.name), [], 5,
                                                 runtime.DEFAULT_MODEL)

    def test_compact_boundary_does_not_replace_a_missing_normal_answer(self):
        proc = self.process([
            {'type': 'system', 'subtype': 'compact_boundary', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'empty response'):
                runtime._run_claude('继续', 'sid', Path(self.tmp.name), [], 5)

    def test_nonzero_exit_is_not_success_even_with_partial_text(self):
        proc = self.process([
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '处理中'}]}},
            {'type': 'result', 'result': '处理中', 'session_id': 'sid'},
        ], returncode=1, stderr='gateway unavailable')
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'gateway unavailable'):
                runtime._run_claude('继续', 'sid', Path(self.tmp.name), [], 5)

    def test_result_errors_override_partial_assistant_text(self):
        proc = self.process([
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '处理中'}]}},
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
             'errors': ['maximum context length exceeded'], 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'maximum context length'):
                runtime._run_claude('继续', 'sid', Path(self.tmp.name), [], 5)

    def test_large_tool_output_triggers_confirmation_on_next_request(self):
        proc = self.process([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'large.txt'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'content': 'x' * 130000}]}},
            {'type': 'result', 'result': '完成', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc) as spawn:
            runtime.run('chat', '读取', sender_id='user', image_refs=[])
            response, stats = runtime.run('chat', '继续', sender_id='user', image_refs=[])
        self.assertIn('确认压缩', response)
        self.assertTrue(stats.get('needs_compaction'))
        self.assertEqual(spawn.call_count, 1)

    def test_result_only_response_is_included_in_persisted_estimate(self):
        proc = self.process([
            {'type': 'result', 'result': 'x' * 130000, 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            runtime.run('chat', '读取', sender_id='user', image_refs=[])
        self.assertGreaterEqual(runtime.sessions[self.scope]['estimated_chars'], 130000)

    def test_aggregate_result_usage_is_not_treated_as_single_context_size(self):
        proc = self.process([
            {'type': 'assistant', 'message': {
                'usage': {'input_tokens': 100, 'output_tokens': 2},
                'content': [{'type': 'text', 'text': '完成'}]}},
            {'type': 'result', 'result': '完成', 'session_id': 'sid',
             'usage': {'input_tokens': 900000}},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            runtime.run('chat', '读取', sender_id='user', image_refs=[])
        self.assertLess(runtime.sessions[self.scope]['estimated_chars'], 120000)

    def test_image_base64_is_not_counted_as_text_context(self):
        proc = self.process([
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'data': 'x' * 150000}},
                {'type': 'text', 'text': '图片'},
            ]}]}},
            {'type': 'result', 'result': '完成', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            runtime.run('chat', '读取', sender_id='user', image_refs=[])
        self.assertLess(runtime.sessions[self.scope]['estimated_chars'], 120000)

    def test_overflow_after_compaction_does_not_create_confirmation_loop(self):
        runtime.sessions[self.scope] = {'active': 'sid', 'history': [], 'estimated_chars': 10}
        proc = self.process([
            {'type': 'result', 'is_error': True, 'result': 'Prompt is too long', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            response, stats = runtime.run('chat', '继续', sender_id='user', image_refs=[],
                                          _skip_compaction=True)
        self.assertIn('分段', response)
        self.assertFalse(stats['ok'])
        self.assertFalse(stats.get('needs_compaction'))
        self.assertNotIn('pending_compaction', runtime.sessions[self.scope])

    def test_cached_input_usage_counts_toward_context_instead_of_final_reply_length(self):
        proc = self.process([
            {'type': 'assistant', 'message': {
                'usage': {'input_tokens': 1000, 'cache_read_input_tokens': 28000,
                          'cache_creation_input_tokens': 2000, 'output_tokens': 2},
                'content': [{'type': 'text', 'text': '完成'}]}},
            {'type': 'result', 'result': '完成', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc) as spawn:
            runtime.run('chat', '读取', sender_id='user', image_refs=[])
            response, stats = runtime.run('chat', '继续', sender_id='user', image_refs=[])
        self.assertIn('确认压缩', response)
        self.assertTrue(stats.get('needs_compaction'))
        self.assertEqual(spawn.call_count, 1)

    def test_overflow_preserves_original_request_and_session_for_confirmation(self):
        runtime.sessions[self.scope] = {'active': 'sid', 'history': [], 'estimated_chars': 10}
        proc = self.process([
            {'type': 'result', 'is_error': True, 'result': 'Prompt is too long', 'session_id': 'sid'},
        ])
        with patch.object(runtime.subprocess, 'Popen', return_value=proc) as spawn:
            response, stats = runtime.run('chat', '继续', sender_id='user', image_refs=[],
                                          group_history_context='检索结果')
        self.assertIn('确认压缩', response)
        self.assertTrue(stats.get('needs_compaction'))
        self.assertEqual(runtime.sessions[self.scope]['active'], 'sid')
        pending = runtime.sessions[self.scope]['pending_compaction']
        self.assertEqual(pending['text'], '继续')
        self.assertEqual(pending['group_history_context'], '检索结果')
        self.assertEqual(spawn.call_count, 1)

    def test_first_turn_overflow_keeps_cli_session_created_before_tool_results(self):
        proc = self.process([
            {'type': 'system', 'subtype': 'init', 'session_id': 'new-sid'},
            {'type': 'assistant', 'error': 'invalid_request', 'message': {
                'content': [{'type': 'text', 'text': 'Prompt is too long'}]}},
            {'type': 'result', 'is_error': True, 'errors': ['Prompt is too long']},
        ], returncode=1)
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            response, stats = runtime.run('chat', '读取', sender_id='user', image_refs=[])
        self.assertIn('确认压缩', response)
        self.assertTrue(stats.get('needs_compaction'))
        self.assertEqual(runtime.sessions[self.scope]['active'], 'new-sid')

    def test_overflow_without_resumable_session_asks_for_smaller_input(self):
        proc = self.process([
            {'type': 'result', 'is_error': True, 'result': 'Prompt is too long'},
        ], returncode=1)
        with patch.object(runtime.subprocess, 'Popen', return_value=proc):
            response, stats = runtime.run('chat', 'x' * 100, sender_id='user', image_refs=[])
        self.assertIn('分段', response)
        self.assertFalse(stats['ok'])
        self.assertNotIn('pending_compaction', runtime.sessions[self.scope])

    def test_failed_compaction_does_not_reset_estimate_or_replay_request(self):
        runtime.sessions[self.scope] = {
            'active': 'sid', 'history': [], 'estimated_chars': 150000,
            'pending_compaction': {'chat_id': 'chat', 'sender_id': 'user', 'text': '继续'},
        }
        proc = self.process([
            {'type': 'result', 'is_error': True, 'result': 'Prompt is too long', 'session_id': 'sid'},
        ], returncode=1)
        with patch.object(runtime.subprocess, 'Popen', return_value=proc) as spawn:
            response, stats = runtime.confirm_compaction('chat', sender_id='user')
        self.assertIn('压缩失败', response)
        self.assertFalse(stats['compacted'])
        self.assertEqual(runtime.sessions[self.scope]['estimated_chars'], 150000)
        self.assertIn('pending_compaction', runtime.sessions[self.scope])
        self.assertEqual(spawn.call_count, 1)

    def test_dev_submit_compaction_uses_saved_request_text(self):
        scope = runtime.conversation_key('chat', 'user', 'p2p', 'dev')
        runtime.sessions[scope] = {
            'active': 'sid', 'history': [],
            'pending_compaction': {'chat_id': 'chat', 'sender_id': 'user', 'text': '继续分析', 'profile': 'dev'},
        }
        with patch.object(runtime, 'resolve_tier', return_value='submit'), \
             patch.object(runtime, '_compact_session', return_value=('sid', 0)), \
             patch.object(runtime, 'run', return_value=('完成', {})):
            response, stats = runtime.confirm_compaction('chat', sender_id='user', profile='dev')
        self.assertEqual(response, '完成')
        self.assertTrue(stats['compacted'])


if __name__ == '__main__':
    unittest.main()
