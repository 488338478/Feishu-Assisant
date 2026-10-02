import base64
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

pkg = types.ModuleType('assistant')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant import handlers
from assistant.feishu.media import MediaStore
from test_message_gate import event, mention


class InlineThread:
    def __init__(self, target, **kwargs): self.target = target
    def start(self): self.target()


class HandlerMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MediaStore(Path(self.temp.name), 'chat', 'sender')
        self.png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1sAAAAASUVORK5CYII=')

    def test_private_image_starts_ai_with_image_ref(self):
        with patch.object(handlers, 'make_media_store', return_value=self.store), \
             patch.object(handlers, 'build_client', return_value=MagicMock()), \
             patch.object(handlers, 'download_message_image', side_effect=lambda *a: self.store.save(self.png)), \
             patch.object(handlers.threading, 'Thread', InlineThread), \
             patch.object(handlers, 'process_message', return_value='图片已读取') as process, \
             patch.object(handlers, 'send_message'), patch.object(handlers, 'add_reaction'), \
             patch.object(handlers, 'delete_reaction'):
            handlers.on_message(event('p2p', message_type='image', raw_content=json.dumps({'image_key': 'img_1'})))
        self.assertEqual(len(process.call_args.kwargs['image_refs']), 1)
        self.assertEqual(process.call_args.args[2], 'sender')

    def test_failed_download_returns_error_before_ai(self):
        with patch.object(handlers, 'make_media_store', return_value=self.store), \
             patch.object(handlers, 'build_client', return_value=MagicMock()), \
             patch.object(handlers, 'download_message_image', side_effect=ValueError('权限不足')), \
             patch.object(handlers.threading, 'Thread', InlineThread), \
             patch.object(handlers, 'process_message') as process, \
             patch.object(handlers, 'send_message') as send, patch.object(handlers, 'add_reaction'), \
             patch.object(handlers, 'delete_reaction'):
            handlers.on_message(event('p2p', message_type='image', raw_content=json.dumps({'image_key': 'img_1'})))
        process.assert_not_called()
        self.assertIn('权限不足', send.call_args.args[2])

    def test_unmentioned_group_image_does_not_download_or_start_ai(self):
        with patch.object(handlers, 'BOT_OPEN_ID', 'bot-id'), \
             patch.object(handlers.group_history_store, 'record'), \
             patch.object(handlers, 'download_message_image') as download, \
             patch.object(handlers.threading, 'Thread') as thread:
            handlers.on_message(event('group', [], message_type='image', raw_content=json.dumps({'image_key': 'img_1'})))
        download.assert_not_called()
        thread.assert_not_called()

    def test_ordinary_quoted_text_still_runs_when_quote_resource_is_unavailable(self):
        request = event('p2p', text='继续讨论代码')
        request.event.message.parent_id = 'quoted'
        with patch.object(handlers, 'make_media_store', return_value=self.store), \
             patch.object(handlers, 'build_client', return_value=MagicMock()), \
             patch.object(handlers, 'quoted_images', side_effect=ValueError('没有消息读取权限')), \
             patch.object(handlers.threading, 'Thread', InlineThread), \
             patch.object(handlers, 'process_message', return_value='继续讨论') as process, \
             patch.object(handlers, 'send_message'), patch.object(handlers, 'add_reaction'), \
             patch.object(handlers, 'delete_reaction'):
            handlers.on_message(request)
        self.assertEqual(process.call_count, 1)

    def test_quoted_deletion_confirmation_skips_optional_quote_fetch(self):
        for verb in ('确认', '取消'):
            request = event('p2p', text=f'{verb}删除图片 ' + 'a' * 32)
            request.event.message.parent_id = 'confirmation-message'
            with patch.object(handlers, 'make_media_store', return_value=self.store), \
                 patch.object(handlers, 'build_client', return_value=MagicMock()), \
                 patch.object(handlers, 'quoted_images', side_effect=ValueError('没有消息读取权限')) as quoted, \
                 patch.object(handlers.threading, 'Thread', InlineThread), \
                 patch.object(handlers, 'process_message', return_value='已处理') as process, \
                 patch.object(handlers, 'send_message'), patch.object(handlers, 'add_reaction'), \
                 patch.object(handlers, 'delete_reaction'):
                handlers.on_message(request)
            quoted.assert_not_called()
            self.assertEqual(process.call_count, 1)


if __name__ == '__main__':
    unittest.main()
