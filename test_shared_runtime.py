"""Verify the actual standalone shared runtime layout, without user data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from install_local import prepare_shared


class SharedRuntimeTests(unittest.TestCase):
    def test_background_imports_catalog_from_shared_directory(self):
        source = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='hub-shared-layout-') as temp:
            root = Path(temp) / 'CodexProviderHub'
            prepare_shared(source, root)
            shared = root / 'shared'
            code = ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
                    "import ocx_service,catalog; from catalog import recover_publication; "
                    "print(pathlib.Path(catalog.__file__).resolve().parent == pathlib.Path(sys.argv[1]).resolve())")
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, '-B', '-c', code, str(shared)], cwd=temp, env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'True')


if __name__ == '__main__':
    unittest.main()
