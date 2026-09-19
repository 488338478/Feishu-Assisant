import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

package = types.ModuleType('assistant')
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', package)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = dict(workspace=self.tmp.name, capabilities=['lark.read', 'lark.write', 'lark.personal', 'file.read', 'file.write', 'p4.read', 'p4.edit', 'p4.submit'], tier='submit', sender='ou_a', user_identity_open_id='ou_a', lark_profile='assistant-bot', p4_client='test-client')

    def adapter(self, **kwargs):
        from assistant.operations.execution import ExecutionAdapter
        return ExecutionAdapter(self.context, **kwargs)

    def test_failure_json_and_exit_code_are_not_success(self):
        for code, output in [(0, '{\n "ok": false, "error": "no"\n}'), (0, '{"code":123}'), (0, '{"success":false}'), (1, '{"ok":true}'), (0, 'broken')]:
            with self.subTest(output=output):
                a = self.adapter(runner=lambda *args, **kwargs: (code, output, ''))
                self.assertFalse(a.call('Bash', {'command':'lark-cli --as bot drive +search --query test'})['success'])

    def test_verified_owner_required_immediately_before_execution(self):
        calls = []
        a = self.adapter(identity_verifier=lambda context: 'ou_other', runner=lambda *a, **k: calls.append(a))
        result = a.call('Bash', {'command':'lark-cli --as user calendar +agenda'})
        self.assertFalse(result['success'])
        self.assertEqual(calls, [])
        a = self.adapter(identity_verifier=lambda context: 'ou_a', runner=lambda *a, **k: (0, '{\n "ok": true, "data": []\n}', ''))
        self.assertTrue(a.call('Bash', {'command':'lark-cli --as user calendar +agenda'})['success'])

    def test_local_write_read_edit_and_cancel(self):
        a = self.adapter()
        self.assertTrue(a.call('Write', {'file_path':'hello.txt', 'content':'hello world'})['success'])
        self.assertTrue(a.call('Edit', {'file_path':'hello.txt','old_string':'world','new_string':'there'})['success'])
        self.assertEqual(a.call('Read', {'file_path':'hello.txt'})['result'], 'hello there')
        self.context['cancelled'] = True
        a = self.adapter()
        self.assertFalse(a.call('Write', {'file_path':'hello.txt', 'content':'oops'})['success'])
        self.assertEqual((Path(self.tmp.name)/'hello.txt').read_text(), 'hello there')

    def test_trace_truthful_redacted_and_correlated(self):
        from assistant.operations.store import OpsStore
        store = OpsStore(Path(self.tmp.name)/'control'/'ops.sqlite3')
        run, _ = store.create_run('chat', 'ou_a', 'test')
        self.context['run_id'] = run['id']
        a = self.adapter(store=store, runner=lambda *a, **k: (0, '{"ok":false,"access_token":"secret-value"}', ''))
        result = a.call('Bash', {'command':'lark-cli --as bot drive +search --query test'})
        events = store.events(run['id'])
        self.assertEqual([e['kind'] for e in events], ['tool_requested','policy_allowed','tool_failed'])
        self.assertEqual({e['payload']['call_id'] for e in events}, {result['call_id']})
        self.assertNotIn('secret-value', json.dumps(events))

    def test_malformed_or_duplicate_mcp_cannot_repeat_write(self):
        from assistant.operations.adapter import serve
        a = self.adapter()
        request = dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(name='call', arguments=dict(name='Write', input=dict(file_path='out.txt',content='first'))))
        second = json.loads(json.dumps(request))
        second['params']['arguments']['input']['content'] = 'second'
        malformed = dict(jsonrpc='2.0', method='tools/call', params=request['params'])
        target = io.StringIO()
        serve(a, io.StringIO('\n'.join(json.dumps(v) for v in [malformed, request, second])+'\n'), target)
        self.assertEqual((Path(self.tmp.name)/'out.txt').read_text(), 'first')
        self.assertIn('error', json.loads(target.getvalue().splitlines()[-1]))

    def test_workspace_lock_lease_and_contention(self):
        from assistant.operations.execution import workspace_lock, validate_workspace_lease
        with workspace_lock(self.tmp.name, timeout=.1) as lease:
            self.assertTrue(validate_workspace_lease(self.tmp.name, lease))
            with self.assertRaises(TimeoutError):
                with workspace_lock(self.tmp.name, timeout=.1):
                    pass
        self.assertFalse(validate_workspace_lease(self.tmp.name, lease))

    def test_hook_denies_builtin_and_accepts_registered_wrapper(self):
        from assistant.operations.hook import evaluate_hook
        self.assertEqual(evaluate_hook({'tool_name':'Bash','tool_input':{'command':'whoami'}}, self.context)['hookSpecificOutput']['permissionDecision'], 'deny')
        allowed = evaluate_hook({'tool_name':'mcp__operations__call','tool_input':{'name':'Read','input':{'file_path':'hello.txt'}}}, self.context)
        self.assertEqual(allowed['hookSpecificOutput']['permissionDecision'], 'allow')


if __name__ == '__main__':
    unittest.main()
