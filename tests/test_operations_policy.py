import sys
import tempfile
import types
import unittest
from pathlib import Path

package = types.ModuleType('assistant')
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', package)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = {'capabilities': ['lark.read','lark.write','file.read','file.write','p4.read'],
                        'workspace': self.tmp.name, 'lark_profile': 'assistant-bot',
                        'sender': 'ou_a', 'user_identity_open_id': ''}

    def check(self, name, inp):
        from assistant.operations.policy import check_tool
        return check_tool(name, inp, self.context)

    def test_registered_read_and_write_are_separate(self):
        self.assertTrue(self.check('Bash', {'command':'lark-cli --profile assistant-bot --as bot drive +search --query test'})['allowed'])
        self.context['capabilities'] = ['lark.read']
        self.assertFalse(self.check('Bash', {'command':'lark-cli --profile assistant-bot --as bot docs +create --title test --content test'})['allowed'])

    def test_escape_and_equivalent_api_write_are_denied(self):
        self.context['capabilities'] = ['lark.read']
        for cmd in ['lark-cli drive +search --query x; whoami',
                    'lark-cli drive +search --query $(whoami)',
                    'lark-cli auth login', 'lark-cli config init',
                    'lark-cli --profile another --as bot drive +search --query x',
                    'lark-cli --as bot api POST /open-apis/docx/v1/documents --data "{}"',
                    'lark-cli --as bot api GET /open-apis/../authen/v1/user_info',
                    'lark-cli --as bot docs +create --content @secret.txt',
                    'python -c "print(1)"']:
            with self.subTest(cmd=cmd): self.assertFalse(self.check('Bash', {'command':cmd})['allowed'])

    def test_personal_identity_requires_matching_owner(self):
        self.context['capabilities'].append('lark.personal')
        cmd = {'command':'lark-cli --profile assistant-bot --as user calendar +agenda'}
        self.assertFalse(self.check('Bash', cmd)['allowed'])
        self.context['user_identity_open_id'] = 'ou_b'
        self.assertFalse(self.check('Bash', cmd)['allowed'])
        self.context['user_identity_open_id'] = 'ou_a'
        self.assertTrue(self.check('Bash', cmd)['allowed'])

    def test_files_must_be_within_workspace_and_cannot_modify_policy(self):
        root = Path(self.tmp.name)
        self.assertTrue(self.check('Read', {'file_path':str(root/'notes.md')})['allowed'])
        self.assertFalse(self.check('Read', {'file_path':str(root.parent/'secret')})['allowed'])
        self.assertFalse(self.check('Write', {'file_path':str(root/'.claude/settings.json')})['allowed'])
        self.assertFalse(self.check('Write', {'file_path':str(root/'CLAUDE.md')})['allowed'])
        self.assertFalse(self.check('Bash', {'command':'p4 submit -d test'})['allowed'])

    def test_unobserved_tool_types_are_denied(self):
        self.assertFalse(self.check('WebFetch', {'url':'https://example.test'})['allowed'])

    def test_flag_schema_and_control_files_fail_closed(self):
        for cmd in ['lark-cli --as bot drive +search --query x --output evil',
                    'lark-cli --as bot drive +search --query @secret',
                    'lark-cli --as bot docs +create --title x --content test --exec evil',
                    'lark-cli --as bot drive +unknown', 'p4 -c other opened',
                    'p4 edit //depot/...', 'p4 -x input edit', 'p4 set P4PORT=evil']:
            self.assertFalse(self.check('Bash', {'command':cmd})['allowed'], cmd)
        for path in ['.env', '.env.local', '.git/config', 'data/credentials.json', 'AGENTS.md', '.codex/config.toml']:
            self.assertFalse(self.check('Read', {'file_path':path})['allowed'], path)
        self.assertFalse(self.check('Read', {'file_path':'notes.txt', 'unknown':1})['allowed'])

    def test_p4_requires_tier_client_and_scoped_paths(self):
        self.context.update(tier='edit', p4_client='my-client')
        self.context['capabilities'].extend(['p4.edit', 'p4.submit'])
        self.assertTrue(self.check('Bash', {'command':'p4 edit notes.txt'})['allowed'])
        self.assertFalse(self.check('Bash', {'command':'p4 submit -d message notes.txt'})['allowed'])
        self.context['tier'] = 'submit'
        self.assertTrue(self.check('Bash', {'command':'p4 submit -d message notes.txt'})['allowed'])
        self.assertFalse(self.check('Bash', {'command':'p4 submit -c 123'})['allowed'])
        self.assertFalse(self.check('Bash', {'command':'p4 edit ../outside'})['allowed'])

    def test_protected_parent_and_symlink_paths(self):
        root = Path(self.tmp.name)
        protected = root/'control'
        protected.mkdir()
        self.context['protected_paths'] = [str(protected)]
        self.assertFalse(self.check('Read', {'file_path':'control/context.json'})['allowed'])
        self.assertFalse(self.check('Write', {'file_path':str(root)})['allowed'])
        try:
            (root/'alias').symlink_to(protected, target_is_directory=True)
        except OSError:
            return
        self.assertFalse(self.check('Read', {'file_path':'alias/context.json'})['allowed'])

if __name__ == '__main__': unittest.main()
