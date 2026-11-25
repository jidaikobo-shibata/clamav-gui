"""Explicit live FreshClam test in a cloned, temporary database and LaunchAgent."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clam_gui.macos_setup import MacSetup
from clam_gui.macos_update import MacAutoUpdateService, read_update
from clam_gui.paths import database_directory


def main():
    with tempfile.TemporaryDirectory(prefix='clam-update-validation-') as directory:
        root = Path(directory)
        backend = MacSetup(root / 'support')
        backend.configure()
        for path in database_directory().iterdir():
            if path.is_file() and (path.suffix in ('.cvd', '.cld', '.cud', '.sign') or path.name == 'freshclam.dat'):
                destination = backend.database / path.name
                clone = subprocess.run(['/bin/cp', '-c', str(path), str(destination)], capture_output=True)
                if clone.returncode:
                    shutil.copy2(path, destination)
        service = MacAutoUpdateService(backend.root, root / 'agents',
            label='com.jidaikobo.ClamAVGui.validation.update')
        try:
            service.set_enabled(True)
            print('Temporary automatic update registered; normal definitions are untouched.', flush=True)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                state = read_update(backend.root)
                if state and state.get('state') in ('completed', 'failed', 'cancelled'):
                    print('FreshClam result:', state['state'], state['message'], flush=True)
                    output = backend.root / 'update-output.log'
                    if output.exists():
                        print(output.read_text()[-2000:], flush=True)
                    if state['state'] != 'completed':
                        raise RuntimeError(state['message'])
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('Automatic update did not finish within four minutes')
            service.set_enabled(False)
            if service.status().enabled:
                raise RuntimeError('Automatic update did not disable')
            print('Automatic update and disable succeeded.', flush=True)
        finally:
            try:
                service.set_enabled(False)
            finally:
                service.agent.remove()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
