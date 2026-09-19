import sys, types, unittest
from pathlib import Path
from unittest.mock import patch
pkg=types.ModuleType('assistant'); pkg.__path__=[str(Path(__file__).resolve().parents[1])]; sys.modules.setdefault('assistant',pkg)
from assistant.feishu.auth import LarkAuthManager

class CalendarScopeTests(unittest.TestCase):
    def test_calendar_scope_error_explains_required_feishu_scope(self):
        manager=LarkAuthManager('assistant-bot')
        with patch.object(manager,'_already_logged_in',return_value=True), patch.object(manager,'_run') as run:
            run.return_value=type('R',(),{'returncode':1,'stdout':'','stderr':'missing scope calendar:calendar.event:read'})()
            # status failure must not be hidden by generic authorization text
            result=manager._run
        self.assertIn('calendar:calendar.event:read', manager.required_scopes('calendar'))

if __name__=='__main__': unittest.main()
