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

    def test_prompt_identifies_the_current_sender_in_shared_group_context(self):
        prompt = runtime._build_prompt('上一位说到哪里了？', 'user-b', 'read', 'docs')
        self.assertIn('[当前消息发送人] open_id=user-b', prompt)

    def test_clear_session_removes_context_and_memory_scope(self):
        key=runtime.session_key('group','user-a','group')
        runtime.sessions[key]={'active':'sid','history':[{'id':'sid'}]}
        runtime._save_sessions()
        with patch.object(runtime.memory,'clear_scope') as clear:
            self.assertTrue(runtime.clear_session('group','user-a','group'))
            clear.assert_called_once_with(key)
        self.assertNotIn(key,runtime.sessions)
        self.assertFalse(runtime.clear_session('group','user-a','group'))

if __name__=='__main__': unittest.main()
