"""Test-only barriers around the real helper transaction, no production hooks."""
import importlib.util
import json
from pathlib import Path
import sys
import time

script, request_file, barrier = sys.argv[1:]
spec=importlib.util.spec_from_file_location('state',script)
state=importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
request=json.loads(Path(request_file).read_text(encoding='utf8'))
barrier=Path(barrier)
original=state.os.replace

def paused_replace(source,destination):
    # The complete snapshot/temp write has happened and BEGIN IMMEDIATE is held.
    (barrier/'ready').write_text(str(state.os.getpid()),encoding='ascii')
    deadline=time.monotonic()+20
    while not (barrier/'release').exists():
        if time.monotonic()>deadline:
            raise TimeoutError()
        time.sleep(.01)
    return original(source,destination)

state.os.replace=paused_replace
print(state.compact({'ok':True,'value':state.run(request)}))
