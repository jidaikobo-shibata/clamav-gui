"""Unprivileged client for the separately installed, root-owned scan broker."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

HELPER = Path('/usr/libexec/clamav-gui-admin-scan')
POLICY = Path('/usr/share/polkit-1/actions/com.jidaikobo.ClamAVGui.admin-scan.policy')
SOURCE = Path(__file__).resolve().parents[1]/'privileged/admin_scan.py'


def trusted(path):
    try:
        for item in (path, *path.parents):
            mode = item.lstat()
            if mode.st_uid != 0 or mode.st_mode & 0o022 or stat.S_ISLNK(mode.st_mode):
                return False
        return path.is_file()
    except OSError:
        return False


def available():
    try:
        return (os.name == 'posix' and trusted(HELPER) and trusted(POLICY)
                and Path('/usr/bin/pkexec').is_file() and Path('/usr/bin/bwrap').is_file()
                and hashlib.sha256(HELPER.read_bytes()).digest() == hashlib.sha256(SOURCE.read_bytes()).digest()
                and POLICY.read_bytes() == (SOURCE.parent/POLICY.name).read_bytes())
    except OSError:
        return False


class AdminProcess:
    """Popen-compatible cancellation: root broker watches this private pipe."""
    def __init__(self, request, **kwargs):
        if not available():
            raise ValueError('管理者検査の補助プログラムが未導入、または更新が必要です。環境設定の「管理者検査」で導入・更新してください。')
        payload = (json.dumps(request, ensure_ascii=True) + '\n').encode()
        if len(payload) > 4096:
            raise ValueError('対象・除外設定が多すぎます。管理者検査のプランを分けてください。')
        self.process = subprocess.Popen(['/usr/bin/pkexec', str(HELPER)], stdin=subprocess.PIPE,
            **kwargs)
        try:
            os.write(self.process.stdin.fileno(), payload)
        except Exception:
            self.terminate()
            self.process.wait(timeout=5)
            raise

    def wait(self, timeout=None):
        code = self.process.wait(timeout=timeout)
        self.process.stdin.close()
        return code

    def poll(self):
        return self.process.poll()

    def terminate(self):
        self.process.stdin.close()
        try:
            self.process.terminate()
        except (PermissionError, ProcessLookupError):
            pass  # Root broker handles EOF; before auth pkexec is signalable.

    def kill(self):
        self.process.stdin.close()
        try:
            self.process.kill()
        except (PermissionError, ProcessLookupError):
            pass


def spawn(plan, settings, limits, **kwargs):
    # pkexec receives no application environment, Python path, or config path.
    kwargs['env'] = {k: os.environ[k] for k in ('DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR') if k in os.environ}
    kwargs['env'].update(PATH='/usr/bin:/bin', LC_ALL='C')
    return AdminProcess(dict(version=1, targets=list(map(str, plan.targets)),
        excludes=list(map(str, plan.excludes)), excluded_dirs=list(map(str, plan.excluded_dirs)),
        options=settings, limits=limits), **kwargs)
