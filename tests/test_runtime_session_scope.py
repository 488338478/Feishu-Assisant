import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

pkg = types.ModuleType('assistant'); pkg.__path__=[str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant.agent import runtime

class SessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'sessions.json'
        self.lock=patch.object(runtime,'SESSIONS_FILE',self.path); self.lock.start(); self.addCleanup(self.lock.stop)
        runtime.sessions.clear(); runtime._chat_locks.clear()

    def test_group_session_is_shared_and_does_not_cross_private(self):
        self.assertEqual(runtime.session_key('group','user-a','group'), 'group:group')
        self.assertEqual(runtime.session_key('group','user-b','group'), 'group:group')
        self.assertEqual(runtime.session_key('chat','user-a','p2p'), 'private:chat')
        self.assertNotEqual(runtime.session_key('chat','user-a','p2p'), runtime.session_key('chat','user-a','group'))

    def test_claude_conversation_is_shared_within_profile_but_isolated_between_profiles(self):
        self.assertEqual(
            runtime.conversation_key('group', 'user-a', 'group', 'docs'),
            runtime.conversation_key('group', 'user-b', 'group', 'docs'),
        )
        self.assertNotEqual(
            runtime.conversation_key('group', 'user-a', 'group', 'docs'),
            runtime.conversation_key('group', 'user-a', 'group', 'dev'),
        )
        self.assertEqual(
            runtime.session_key('group', 'user-a', 'group'),
            'group:group',
        )

    def test_prompt_identifies_the_current_sender_in_shared_group_context(self):
        prompt = runtime._build_prompt('上一位说到哪里了？', 'user-b', 'read', 'docs')
        self.assertIn('[当前消息发送人] open_id=user-b', prompt)

    def test_clear_session_removes_context_and_memory_scope(self):
        key=runtime.session_key('group','user-a','group')
        runtime.sessions[key]={'active':'sid','history':[{'id':'sid'}]}
        runtime.sessions[f'{key}:profile:docs']={'active':'docs-sid','history':[]}
        runtime.sessions[f'{key}:profile:dev']={'active':'dev-sid','history':[]}
        runtime.sessions['group:other:profile:docs']={'active':'other-sid','history':[]}
        runtime._save_sessions()
        with patch.object(runtime.memory,'clear_scope') as clear:
            self.assertTrue(runtime.clear_session('group','user-a','group'))
            clear.assert_called_once_with(key)
        self.assertNotIn(key,runtime.sessions)
        self.assertNotIn(f'{key}:profile:docs',runtime.sessions)
        self.assertNotIn(f'{key}:profile:dev',runtime.sessions)
        self.assertIn('group:other:profile:docs',runtime.sessions)
        self.assertFalse(runtime.clear_session('group','user-a','group'))

    def test_missing_resumed_conversation_is_cleared_and_retried_once(self):
        cwd = Path(self.tmp.name)
        conversation_key = runtime.conversation_key('group', 'user-b', 'group', 'docs')
        runtime.sessions[conversation_key] = {'active': 'stale', 'history': [{'id': 'stale'}]}
        with patch.object(runtime, '_profile_cwd', return_value=cwd), \
             patch.object(runtime, 'resolve_profile', return_value='docs'), \
             patch.object(runtime, 'resolve_tier', return_value='read'), \
             patch.object(runtime, 'resolve_model', return_value=runtime.DEFAULT_MODEL), \
             patch.object(runtime, '_audit'), \
             patch.object(runtime, '_run_claude', side_effect=[
                 RuntimeError('No conversation found with session ID: stale'),
                 ('recovered', 'fresh', 2),
             ]) as run_claude:
            result, stats = runtime.run('group', '继续', sender_id='user-b', chat_type='group')
        self.assertEqual(result, 'recovered')
        self.assertTrue(stats['ok'])
        self.assertTrue(stats['session_recovered'])
        self.assertEqual(run_claude.call_count, 2)
        self.assertEqual(run_claude.call_args_list[0].args[1], 'stale')
        self.assertIsNone(run_claude.call_args_list[1].args[1])
        self.assertEqual(runtime.sessions[conversation_key]['active'], 'fresh')

    def test_other_runtime_error_is_not_retried(self):
        cwd = Path(self.tmp.name)
        conversation_key = runtime.conversation_key('group', 'user-b', 'group', 'docs')
        runtime.sessions[conversation_key] = {'active': 'stale', 'history': []}
        with patch.object(runtime, '_profile_cwd', return_value=cwd), \
             patch.object(runtime, 'resolve_profile', return_value='docs'), \
             patch.object(runtime, 'resolve_tier', return_value='read'), \
             patch.object(runtime, 'resolve_model', return_value=runtime.DEFAULT_MODEL), \
             patch.object(runtime, '_audit'), \
             patch.object(runtime, '_run_claude', side_effect=RuntimeError('gateway unavailable')) as run_claude:
            result, stats = runtime.run('group', '继续', sender_id='user-b', chat_type='group')
        self.assertEqual(result, '处理出错：gateway unavailable')
        self.assertFalse(stats['ok'])
        self.assertFalse(stats['session_recovered'])
        self.assertEqual(run_claude.call_count, 1)
        self.assertEqual(runtime.sessions[conversation_key]['active'], 'stale')

if __name__=='__main__': unittest.main()
