"""Linux implementations behind the common backend entry point."""
from pathlib import Path
import subprocess
import os
import sys


class FileLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        if self.file:
            return True
        import fcntl
        file = self.path.open('a')
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        except BaseException:
            file.close()
            raise
        self.file = file
        return True

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


class LinuxBackend:
    name = 'linux'

    def definition_label(self):
        from ..definitions import definition_update_label
        return definition_update_label()

    def definition_status(self):
        from ..notifications import definition_status
        return definition_status()

    def scheduler(self, store):
        from ..schedule import Scheduler
        return Scheduler(store)

    def packages(self):
        from ..packages import AptManager
        return AptManager()

    def can_update(self, installed):
        return 'clamav-freshclam' in installed

    def updater(self):
        from ..definitions import DefinitionUpdater
        return DefinitionUpdater()

    def auto_update(self):
        from ..definitions import AutoUpdateService
        return AutoUpdateService()

    def background(self, store):
        from ..background import BackgroundService
        return BackgroundService(store)

    def observers(self, engine):
        from ..gio_watch import Observers
        return Observers(engine)

    def lock(self, path):
        return FileLock(path)

    def spawn_scanner(self, command, *, lock, **kwargs):
        if not lock.file:
            raise RuntimeError('検査の実行ロックを取得していません。')
        descriptor = lock.file.fileno()
        helper = Path(__file__).with_name('scanner_guard.py')
        args = [sys.executable, str(helper), '--parent', str(os.getpid()),
                '--lock-fd', str(descriptor), '--', *command]
        return subprocess.Popen(args, pass_fds=(descriptor,), **kwargs)

    def process_identity(self, pid):
        try:
            text = Path(f'/proc/{pid}/stat').read_text()
            fields = text[text.rindex(')') + 2:].split()
            if fields[0] in ('Z', 'X'):
                return ''
            return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
        except FileNotFoundError:
            return '' if Path('/proc').is_dir() else None
        except (OSError, ValueError, IndexError):
            return None

    def notify(self, title, body, urgency='normal', *, target=''):
        from ..desktop_notifications import send
        send(title, body, urgency, target=target)

    def install_stop_handlers(self, callback):
        import signal
        from gi.repository import GLib
        return [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (callback(), True)[1])
                for sig in (signal.SIGTERM, signal.SIGINT)]
