"""Windows 11 backend for the GTK application and its headless runners."""
import datetime
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from ..definitions import AutoUpdateState, CHECK_INTERVAL, CHECK_TIMEOUT, definition_update_label
from ..engine import database, definitions_present, executable, legacy_database, shared_database, startup_error
from ..notifications import definition_status
from .windows_tasks import Scheduler, Task, trigger_for


@dataclass
class PackagePlan:
    action: str
    changes: tuple
    detail: str

    def describe(self):
        if self.action == 'remove':
            return 'ClamAVを削除します。他のアプリからも利用できなくなります。検査履歴とユーザー用定義は保持します。'
        return 'Windows Package ManagerでCiscoのClamAVを導入します。OSの管理者認証が必要になる場合があります。'


class Packages:
    package_names = ('ClamAV', 'FreshClam')

    def status(self):
        return {label: '導入済み' for label, name in zip(self.package_names, ('clamscan','freshclam')) if executable(name)}

    def preview(self, action):
        if action not in ('install', 'remove'):
            raise ValueError('不明な操作です。')
        needed = set(self.package_names) - self.status().keys() if action == 'install' else self.status().keys()
        return PackagePlan(action, tuple(needed), 'winget / Cisco.ClamAV / Cisco Systems, Inc.')

    def execute(self, plan):
        if plan.action not in ('install', 'remove'):
            raise ValueError('不明な操作です。')
        command = 'install' if plan.action == 'install' else 'uninstall'
        winget = Path(os.environ['LOCALAPPDATA'])/'Microsoft/WindowsApps/winget.exe'
        args = [str(winget), command, '--id', 'Cisco.ClamAV', '--exact', '--source', 'winget', '--accept-source-agreements']
        if command == 'install':
            args.append('--accept-package-agreements')
        result = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if command == 'install' and result.returncode == 0:
            from .windows_engine import prepare_install
            prepare_install()
        return result.stdout + result.stderr, result.returncode


class Updates:
    def update(self, cancel=None, progress=None, scheduled=False, timeout=None, **kwargs):
        from .windows_process import DefinitionLock, JobProcess
        binary = executable('freshclam')
        if not binary:
            raise ValueError('FreshClamが見つかりません。')
        folder = shared_database()
        lock = DefinitionLock(folder, shared=False)
        ready = folder.parent/'gui-definitions-ready'
        marker = folder.parent/'freshclam-success'
        deadline = time.monotonic() + timeout if timeout is not None else None
        lock_deadline = deadline or (time.monotonic() + CHECK_TIMEOUT)

        def interrupted():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('定義更新の待機時間を超えました。')
            return cancel and cancel.is_set()

        def acquire(definition_lock):
            while not definition_lock.acquire():
                if not scheduled:
                    raise ValueError('検査または定義更新が実行中です。終了後に再試行してください。')
                if interrupted():
                    return False
                if time.monotonic() >= lock_deadline:
                    raise TimeoutError('定義更新の待機時間を超えました。')
                time.sleep(0.5)
            return True

        if not acquire(lock):
            return '定義更新を中止しました。', ''
        try:
            if interrupted():
                return '定義更新を中止しました。', ''
            if scheduled and ready.is_file() and marker.is_file() and 0 <= time.time() - marker.stat().st_mtime < CHECK_INTERVAL and definitions_present(folder):
                return '1時間以内に定義の更新確認が成功しています。', ''
            with tempfile.TemporaryDirectory(dir=folder.parent) as tmp, tempfile.TemporaryFile() as log:
                target = folder
                if not ready.is_file() and (not folder.exists() or not any(folder.iterdir())):
                    target = Path(tmp)/'database'
                    target.mkdir()
                    legacy = legacy_database()
                    if definitions_present(legacy):
                        source_lock = DefinitionLock(legacy, shared=True)
                        if not acquire(source_lock):
                            return '定義更新を中止しました。', ''
                        try:
                            for source in legacy.iterdir():
                                if source.is_file():
                                    shutil.copy2(source, target/source.name)
                        finally:
                            source_lock.close()
                target.mkdir(exist_ok=True, parents=True)
                config = Path(tmp)/'freshclam.conf'
                config.write_text('DatabaseMirror database.clamav.net\n', encoding='ascii')
                commands = [[binary, '--config-file=' + str(config), '--datadir=' + str(target), '--stdout']]
                if not ready.is_file():
                    scanner = executable('clamscan')
                    if not scanner:
                        raise ValueError('定義の確認に必要なclamscanが見つかりません。')
                    probe = Path(tmp)/'probe.txt'
                    probe.write_bytes(b'ClamAV database load check\n')
                    commands.append([scanner, '--database=' + str(target), '--fail-if-cvd-older-than=3', str(probe)])
                for cmd in commands:
                    if interrupted():
                        return '定義更新を中止しました。', ''
                    p = JobProcess(cmd, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        while True:
                            try:
                                code = p.wait(0.5)
                                break
                            except subprocess.TimeoutExpired:
                                if interrupted():
                                    return '定義更新を中止しました。', ''
                    finally:
                        p.terminate()
                        p.wait()
                    if code:
                        break
                size = log.seek(0, 2)
                log.seek(max(0, size - 65536))
                output = log.read().decode('utf-8', errors='replace')
                if code:
                    raise RuntimeError(startup_error(code) or output or f'{Path(cmd[0]).name} 終了コード {code}')
                if not definitions_present(target):
                    raise RuntimeError('更新後の定義ファイルを確認できません。')
                if target != folder:
                    if folder.exists():
                        folder.rmdir()  # Only replace an empty destination.
                    target.rename(folder)
                ready.touch()
                marker.touch()
                return '定義ファイルの更新確認が完了しました。', output
        finally:
            lock.close()


class AutoUpdates:
    def __init__(self):
        from ..core import Store
        self.task = Task(Store(), 'freshclam')

    def status(self):
        active = bool(self.task.control('query'))
        running = active and bool(self.task.control('running'))
        return AutoUpdateState(active, running, active or bool(executable('freshclam')), 24,
            'ログイン中に毎時、ユーザー用定義の更新を確認します。')

    def set_enabled(self, enabled):
        from ..schedule import Schedule
        if enabled:
            trigger = trigger_for(Schedule(frequency='hourly', minute=datetime.datetime.now().minute))
            self.task.control('register', self.task.xml(trigger, 'windows_update.py', []))
        else:
            self.task.control('remove')
        return '自動更新の設定を変更しました。', self.status()


class Background:
    def __init__(self, store):
        self.store = store
        self.task = Task(store, 'monitor')
        self.path = self.task.name

    def supported(self):
        return True

    def reconcile(self):
        from ..background import requested, enabled, heartbeat
        from ..runtime import activities
        want = requested(self.store)
        if want == enabled(self.store) and (not want or heartbeat(self.store)):
            return '自動検査：稼働中' if want else '自動検査：無効'
        if activities(self.store):
            return '検査完了後に自動検査の状態を変更します。'
        self.apply(want)
        return '自動検査の設定を反映しました。'

    def apply(self, enable):
        old = self.store.preferences.get('background-enabled', False)
        previous = self.task.control('query')
        if enable:
            trigger = ET.Element('LogonTrigger')
            # Restrict logon events to the interactive principal.
            ET.SubElement(trigger, 'UserId').text = os.environ['USERDOMAIN'] + chr(92) + os.environ['USERNAME']
            xml = self.task.xml(trigger, 'run_monitor.py', ['--data-dir', str(self.store.path.parent)])
            self.task.control('register', xml)
        else:
            self.task.control('remove')
        try:
            if enable and old and self.task.control('running'):
                self.store.preferences.set('background-enabled', False)
                deadline = time.monotonic() + 30
                while self.task.control('running'):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('常駐監視の停止を確認できません。')
                    time.sleep(0.3)
            self.store.preferences.set('background-enabled', enable)
            if enable:
                self.task.control('run')
        except Exception:
            self.store.preferences.set('background-enabled', old)
            self.task.control('register', previous) if previous else self.task.control('remove')
            raise


class WindowsBackend:
    name = 'win32'
    scheduler = staticmethod(Scheduler)
    background = staticmethod(Background)
    packages = staticmethod(Packages)
    updater = staticmethod(Updates)
    auto_update = staticmethod(AutoUpdates)

    def definition_status(self):
        return definition_status(database())

    def definition_label(self):
        return definition_update_label(database())

    def can_update(self, installed):
        return 'FreshClam' in installed

    def observers(self, engine):
        from .windows_watch import WindowsObservers
        return WindowsObservers(engine)

    def lock(self, path):
        from .windows_process import FileLock
        return FileLock(path)

    def spawn_scanner(self, command, *, lock, **kwargs):
        from .windows_process import JobProcess
        if not lock.file:
            raise RuntimeError('検査ロックを取得できません。')
        return JobProcess(command, **kwargs)

    def process_identity(self, pid):
        from .windows_process import identity
        return identity(pid)

    def install_stop_handlers(self, callback):
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: callback())

    def notify(self, title, body, urgency='normal'):
        script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
try {
    $n.Icon = [Drawing.SystemIcons]::Information
    $n.Visible = $true
    $n.ShowBalloonTip(5000,$env:CLAM_GUI_TITLE,$env:CLAM_GUI_BODY,$env:CLAM_GUI_ICON)
    Start-Sleep -Seconds 5
} finally { $n.Dispose() }
"""
        shell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive','-Command',script],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, 'CLAM_GUI_TITLE':title, 'CLAM_GUI_BODY':body, 'CLAM_GUI_ICON':'Warning' if urgency == 'critical' else 'Info'})
        if result.returncode:
            raise RuntimeError('Windowsの通知を表示できませんでした。')
