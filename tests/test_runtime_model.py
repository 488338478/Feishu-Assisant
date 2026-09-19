import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

package = types.ModuleType('assistant')
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', package)
from assistant.agent import runtime


class FixedModelTests(unittest.TestCase):
    def test_new_and_resumed_calls_override_inherited_expensive_models(self):
        for session in (None, 'old-pro-session'):
            with self.subTest(session=session):
                proc = Mock(returncode=0)
                proc.stdout = io.StringIO(json.dumps({'type': 'result', 'result': 'ok', 'session_id': 's'}) + '\n')
                proc.stderr = io.StringIO('')
                with patch.dict('os.environ', {'ANTHROPIC_MODEL': 'deepseek-v4-pro',
                                              'ANTHROPIC_DEFAULT_OPUS_MODEL': 'deepseek-v4-pro',
                                              'CLAUDE_CODE_SUBAGENT_MODEL': 'deepseek-v4-pro'}), \
                     patch.object(runtime.subprocess, 'Popen', return_value=proc) as popen:
                    runtime._run_claude('hi', session, Path.cwd(), [], 5)
                cmd = popen.call_args.args[0]
                self.assertIn('--model', cmd)
                self.assertEqual(cmd[cmd.index('--model') + 1], 'deepseek-v4-flash')
                self.assertNotIn('--fallback-model', cmd)
                child_env = popen.call_args.kwargs['env']
                for key in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                            'CLAUDE_CODE_SUBAGENT_MODEL'):
                    self.assertEqual(child_env[key], 'deepseek-v4-flash')
                if session:
                    self.assertEqual(cmd[cmd.index('--resume') + 1], session)

    def test_selected_model_applies_to_new_and_subtask_environment(self):
        proc = Mock(returncode=0)
        proc.stdout = io.StringIO(json.dumps({'type': 'result', 'result': 'ok', 'session_id': 's'}) + '\n')
        proc.stderr = io.StringIO('')
        with patch.object(runtime.subprocess, 'Popen', return_value=proc) as popen:
            runtime._run_claude('hi', None, Path.cwd(), [], 5, model='deepseek-v4-pro')

        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--model') + 1], 'deepseek-v4-pro')
        self.assertEqual(popen.call_args.kwargs['env']['ANTHROPIC_MODEL'], 'deepseek-v4-pro')
