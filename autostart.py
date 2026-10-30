"""Read-only verification of the user-owned hidden startup task."""
from pathlib import Path


def matches_task(task, root: Path, current_sid: str, resolve_sid) -> bool:
    definition = task.Definition
    actions = definition.Actions
    if not task.Enabled or actions.Count != 1:
        return False
    action = actions.Item(1)
    executable = str(root / "runtime/Scripts/pythonw.exe")
    argument = '"' + str(root / "shared/ocx_service.py") + '" serve'
    principal = definition.Principal
    if principal.LogonType != 3 or resolve_sid(principal.UserId) != current_sid:
        return False
    triggers = definition.Triggers
    logons = [triggers.Item(i) for i in range(1, triggers.Count+1) if triggers.Item(i).Type == 9 and triggers.Item(i).Enabled]
    return (str(action.Path).casefold() == executable.casefold() and str(action.Arguments) == argument
            and len(logons) == 1 and bool(logons[0].UserId) and resolve_sid(logons[0].UserId) == current_sid)


def startup_verified(root: Path) -> bool:
    initialized = False
    scheduler = task = None
    try:
        import pythoncom
        import win32com.client
        import win32api
        import win32con
        import win32security
        pythoncom.CoInitialize()
        initialized = True
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        current = win32security.ConvertSidToStringSid(sid)
        def resolve_sid(name):
            parsed = win32security.ConvertStringSidToSid(name) if str(name).startswith("S-1-") else win32security.LookupAccountName(None, name)[0]
            return win32security.ConvertSidToStringSid(parsed)
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        task = scheduler.GetFolder("\\").GetTask("CodexProviderHub")
        return matches_task(task, root, current, resolve_sid)
    except Exception:
        return False
    finally:
        if initialized:
            task = None
            scheduler = None
            pythoncom.CoUninitialize()
