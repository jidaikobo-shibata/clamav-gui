"""Native Windows process lifetime contracts, exercised without ClamAV or GTK."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows kernel objects')
class WindowsProcessTests(unittest.TestCase):
    def test_lock_and_identity(self):
        from clam_gui.platforms.windows_process import FileLock, identity
        with tempfile.TemporaryDirectory() as tmp:
            first = FileLock(Path(tmp)/'test.lock')
            second = FileLock(first.path)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.close()
                self.assertTrue(second.acquire())
                self.assertTrue(identity(os.getpid()))
            finally:
                first.close()
                second.close()

    def test_owned_scanner_exits_on_cancel_or_owner_death(self):
        from clam_gui.platforms.windows_process import identity
        owner_code = """
from clam_gui.platforms.windows_process import JobProcess
import sys
child = JobProcess([sys.executable, '-c', sys.argv[2], sys.argv[1]])
if sys.stdin.readline():
    child.terminate()
child.wait()
"""
        child_code = """
import sys, time, os
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""
        for crash in (False, True):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as tmp:
                ready = Path(tmp)/'child.pid'
                owner = subprocess.Popen([sys.executable, '-c', owner_code, str(ready), child_code],
                                         stdin=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 8
                    while not ready.exists() or not ready.stat().st_size:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    pid = int(ready.read_text())
                    self.assertTrue(identity(pid))
                    if crash:
                        owner.kill()
                    else:
                        owner.stdin.write(b'cancel\n')
                        owner.stdin.flush()
                    owner.wait(5)
                    while identity(pid):
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                finally:
                    owner.stdin.close()
                    if owner.poll() is None:
                        owner.kill()
                        owner.wait()
