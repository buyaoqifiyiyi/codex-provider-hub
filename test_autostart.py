from pathlib import Path
from types import SimpleNamespace as Obj
import unittest

from autostart import matches_task


class Collection:
    def __init__(self, values): self.values=values;self.Count=len(values)
    def Item(self, index): return self.values[index-1]


class StartupTests(unittest.TestCase):
    def test_requires_current_user_for_both_principal_and_logon_trigger(self):
        root=Path(r'C:\fixture\CodexProviderHub')
        action=Obj(Path=str(root/'runtime/Scripts/pythonw.exe'),Arguments='"'+str(root/'shared/ocx_service.py')+'" serve')
        trigger=Obj(Type=9,Enabled=True,UserId='current')
        principal=Obj(LogonType=3,UserId='current')
        task=Obj(Enabled=True,Definition=Obj(Actions=Collection([action]),Triggers=Collection([trigger]),Principal=principal))
        resolve=lambda name: {'current':'S-1-current','other':'S-1-other'}.get(name)
        self.assertTrue(matches_task(task,root,'S-1-current',resolve))
        principal.UserId='other';self.assertFalse(matches_task(task,root,'S-1-current',resolve))
        principal.UserId='current';trigger.UserId='other';self.assertFalse(matches_task(task,root,'S-1-current',resolve))
        trigger.UserId='current';principal.LogonType=5;self.assertFalse(matches_task(task,root,'S-1-current',resolve))


if __name__ == '__main__': unittest.main()
