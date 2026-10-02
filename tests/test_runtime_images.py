import io
import json
import base64
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

pkg = types.ModuleType('assistant')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant.agent import runtime
from assistant.agent.image_mcp import dispatch
from assistant.feishu.media import MediaStore


class RuntimeImageTests(unittest.TestCase):
    def test_real_stdio_subprocess_returns_original_image_without_local_path_access(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1sAAAAASUVORK5CYII=')
        with tempfile.TemporaryDirectory() as directory:
            store = MediaStore(Path(directory) / 'media' / 'inbox', 'chat', 'sender')
            ref = store.save(png)['ref']
            env = os.environ.copy()
            env.update(ASSISTANT_DATA_DIR=directory, ASSISTANT_IMAGE_CHAT_ID='chat', ASSISTANT_IMAGE_SENDER_ID='sender')
            requests = [dict(jsonrpc='2.0', id=1, method='initialize', params={}),
                        dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='read_image', arguments=dict(ref=ref))),
                        dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(name='read_image', arguments=dict(ref='/etc/passwd')))]
            proc = subprocess.run([sys.executable, str(Path(runtime.__file__).with_name('image_mcp.py'))],
                                  input='\n'.join(json.dumps(r) for r in requests) + '\n',
                                  capture_output=True, text=True, encoding='utf-8', env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            results = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(base64.b64decode(results[1]['result']['content'][0]['data']), png)
            self.assertTrue(results[2]['result']['isError'])

    def test_prompt_exposes_refs_without_binary_data_or_paths(self):
        with patch.object(runtime.memory, 'format_context', return_value=''):
            prompt = runtime._build_prompt('看图片', 'sender', 'read', 'docs',
                                           image_refs=['a' * 32])
        self.assertIn('a' * 32, prompt)
        self.assertIn('mcp__assistant_image__read_image', prompt)
        self.assertNotIn('base64', prompt)

    def test_mcp_tool_list_exposes_original_reader_and_delete_preparation_not_execution(self):
        result = dispatch({'id': 1, 'method': 'tools/list'}, None)
        names = {tool['name'] for tool in result['result']['tools']}
        self.assertTrue({'read_image', 'list_document_images', 'read_document_image',
                         'insert_image', 'prepare_delete_image'} <= names)
        self.assertNotIn('confirm_delete', names)

    def test_cli_receives_image_mcp_context_even_without_message_attachment(self):
        captured = {}
        class Process:
            stdout = io.StringIO('{"type":"result","result":"ok","session_id":"session"}\n')
            stderr = io.StringIO('')
            returncode = 0
            def wait(self): pass
        def spawn(command, **kwargs):
            captured.update(command=command, env=kwargs['env'])
            mcp_path = command[command.index('--mcp-config') + 1]
            captured['mcp'] = json.loads(Path(mcp_path).read_text())
            return Process()
        with patch.object(runtime.subprocess, 'Popen', side_effect=spawn):
            result = runtime._run_claude('查看文档配图', None, Path('.'), [], 10,
                permission_context={'chat_id': 'chat', 'sender_id': 'sender', 'profile': 'docs'})
        self.assertEqual(result[0], 'ok')
        self.assertIn('assistant_image', captured['mcp']['mcpServers'])
        self.assertEqual(captured['env']['ASSISTANT_IMAGE_CHAT_ID'], 'chat')
        self.assertEqual(captured['env']['ASSISTANT_IMAGE_SENDER_ID'], 'sender')


if __name__ == '__main__':
    unittest.main()
