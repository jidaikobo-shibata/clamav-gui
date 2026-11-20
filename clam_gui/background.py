"""User-session service management and private cross-process monitoring state."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from .platforms import get_backend
from .schedule import MARKER, Scheduler, quote, read_schedules


def preference(store, key, default=None):
    return store.preferences.get(key, default)


def put_preference(store, key, value):
    store.preferences.set(key, value)


def enabled(store):
    return preference(store, 'background-enabled', False) is True


def watched_plans(store):
    from .watch_scope import safe_watch_plan
    plans = []
    schedules = read_schedules(store)
    for key, plan in store.profiles():
        if plan.kind != 'watch' or not (schedules.get(key) and schedules[key].enabled) or plan.requires_admin:
            continue
        try:
            plans.append((key, safe_watch_plan(plan, store.path.parent)))
        except (ValueError, OSError, RuntimeError):
            # Old/imported profiles receive the same protection as newly saved ones.
            continue
    return plans


class Lease:
    """One monitoring owner across GUI, service, and duplicate launches."""
    def __init__(self, store):
        self.path = store.path.parent/'monitor.lock'
        self.lock = get_backend().lock(self.path)

    @property
    def file(self):
        return self.lock.file

    def acquire(self):
        return self.lock.acquire()

    def close(self):
        self.lock.close()


def heartbeat(store):
    value = preference(store, 'background-state', {})
    if not isinstance(value, dict) or time.time() - value.get('updated', 0) > 8:
        return {}
    return value


def request_media(store):
    token = uuid.uuid4().hex
    put_preference(store, 'background-command', dict(token=token, created=time.time(), action='media'))
    return token


class BackgroundService:
    def __init__(self, store, unit_dir=None, run=None):
        self.store = store
        self.unit_dir = Path(unit_dir) if unit_dir else Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))/'systemd/user'
        self.name = 'clam-gui-monitor-' + hashlib.sha256(str(store.path.resolve()).encode()).hexdigest()[:16] + '.service'
        self.path = self.unit_dir/self.name
        self.run = run or self._run

    @staticmethod
    def _run(*args):
        result = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or 'systemdの操作に失敗しました。').strip())
        return result

    def supported(self):
        return os.name == 'posix' and Path('/run/systemd/system').exists() and bool(shutil.which('systemctl'))

    def unit(self):
        runner = Path(__file__).resolve().parents[1]/'scripts/run_monitor.py'
        args = ['/usr/bin/python3', str(runner), '--data-dir', str(self.store.path.parent)]
        return (MARKER + '[Unit]\nDescription=ClamAV GUI session monitoring\n'
                'PartOf=graphical-session.target\nAfter=graphical-session-pre.target\n\n'
                '[Service]\nType=simple\nExecStart=' + ' '.join(map(quote, args)) +
                '\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=20\nUMask=0077\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\nCPUWeight=20\nIOWeight=20\n'
                '\n[Install]\nWantedBy=graphical-session.target\n')

    def apply(self, enable):
        if type(enable) is not bool:
            raise ValueError('常駐監視の設定が不正です。')
        if self.path.is_symlink():
            raise ValueError('サービス設定がシンボリックリンクのため変更できません。')
        previous = self.path.read_text() if self.path.exists() else None
        if previous is not None and not previous.startswith(MARKER):
            raise ValueError('アプリ以外が作成したサービス設定は変更できません。')
        old = enabled(self.store)
        try:
            if enable:
                self.unit_dir.mkdir(parents=True, exist_ok=True)
                Scheduler.write(self.path, self.unit())
                self.run('daemon-reload')
                put_preference(self.store, 'background-enabled', True)
                self.run('enable', '--now', self.name)
                self.run('restart', self.name)
            else:
                if previous is not None:
                    self.run('disable', '--now', self.name)
                put_preference(self.store, 'background-enabled', False)
        except Exception as error:
            try:
                put_preference(self.store, 'background-enabled', old)
                if previous is None:
                    if self.path.exists():
                        self.run('disable', '--now', self.name)
                        self.path.unlink()
                else:
                    Scheduler.write(self.path, previous)
                self.run('daemon-reload')
                if old:
                    self.run('enable', '--now', self.name)
            except Exception as recovery:
                raise RuntimeError('サービス設定の復元も完了できませんでした。環境設定で状態を確認してください：' + str(recovery)) from error
            raise
