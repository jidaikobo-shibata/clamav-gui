"""Adapt macOS setup and launchd services to the shared GUI backend."""
import subprocess
from .linux import FileLock
from ..macos_schedule import MacScheduler
from ..macos_process import process_identity
from ..paths import database_directory, find_clamav
from ..definitions import definition_update_label
from ..notifications import definition_status


class MacBackend:
    name = 'darwin'
    scheduler = staticmethod(MacScheduler)
    def auto_update(self):
        from ..macos_update import MacAutoUpdateService
        return MacAutoUpdateService()
    lock = staticmethod(FileLock)
    process_identity = staticmethod(process_identity)

    def updater(self):
        return MacScheduledUpdater()

    def packages(self):
        return self

    def engine_update_status(self):
        binary = find_clamav('clamscan')
        if not binary:
            return 'ClamAV本体：未導入', ''
        result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=10)
        return result.stdout.strip(), '検査の準備・更新で確認してください。'

    def definition_label(self):
        return definition_update_label(database_directory())

    def definition_status(self):
        return definition_status(database_directory())

    def observers(self, engine):
        from ..gio_watch import Observers
        return Observers(engine)

    def spawn_scanner(self, command, *, lock, **kwargs):
        return subprocess.Popen(command, pass_fds=(lock.file.fileno(),), **kwargs)

    def notify(self, title, body, urgency='normal'):
        from ..macos_notifications import send
        if not send(title, body):
            raise RuntimeError('macOSの通知を送れませんでした。')


class MacScheduledUpdater:
    def update(self, cancel=None, timeout=180, **kwargs):
        from ..macos_update import MacAutoUpdateService, read_update, run_update
        from ..paths import mac_data_directory
        from ..definitions import CHECK_INTERVAL, daily_definition_age
        from ..engine import definitions_present
        import time
        root = mac_data_directory()
        data = read_update(root)
        if data and data.get('state') == 'completed' and 0 <= time.time() - data.get('finished', 0) < CHECK_INTERVAL and definitions_present(database_directory()) and daily_definition_age(database_directory()) is not None:
            return '1時間以内に定義の更新確認が成功しています。', ''
        service = MacAutoUpdateService(root)
        if service.status().enabled:
            return service.update(cancel=cancel, timeout=timeout), ''
        run_update(cancel=cancel, root=root, timeout=timeout)
        data = read_update(root)
        if not data or data.get('state') != 'completed':
            raise ValueError((data or {}).get('message', '定義更新の完了を確認できませんでした。'))
        return data['message'], ''
