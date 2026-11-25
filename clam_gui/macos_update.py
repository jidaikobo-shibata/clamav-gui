"""One-shot FreshClam jobs and observed, cooperative manual updates on macOS."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .definitions import AutoUpdateState
from .launchd import LaunchAgent, atomic_write, helper_command
from .macos_setup import MacSetup, SetupCancelled, SetupError
from .paths import mac_data_directory
from .runtime import process_identity


@dataclass(frozen=True)
class MacAutoUpdateState(AutoUpdateState):
    def label(self):
        if self.enabled is None:
            return '自動更新：確認できません'
        if self.enabled:
            return '自動更新：有効（' + ('更新中' if self.running else '待機中') + '）'
        return '自動更新：無効' + ('（更新中）' if self.running else '')


def read_update(root):
    path = Path(root) / 'update-state.json'
    if not path.is_file() or path.is_symlink():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('token'), str):
        raise ValueError('更新状態の記録を読み取れません。')
    return data


def live_update(data):
    return bool(data and data.get('state') == 'running' and data.get('identity') and
                process_identity(data.get('pid', -1)) == data['identity'])


def run_update(cancel=None, root=None, timeout=None):
    root = Path(root) if root else mac_data_directory()
    backend = MacSetup(root)
    stop = cancel or threading.Event()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    data = {'token': token, 'pid': os.getpid(), 'identity': process_identity(os.getpid()),
            'started': time.time(), 'state': 'running', 'message': '検査用データを更新しています…'}
    # A separate worker lock prevents overwriting the status of another update.
    import fcntl
    descriptor = os.open(root / 'update-worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        atomic_write(root / 'update-state.json', json.dumps(data, ensure_ascii=False).encode())
        try:
            state = backend.inspect()
            if not state.engine or not state.configuration:
                raise SetupError('アプリを開いてClamAVの準備を完了してください。自動でインストーラーは開きません。')
            if timeout is None:
                backend.prepare(stop, force_update=True)
            else:
                with backend.locked():
                    backend.update_definitions(stop, lambda *_: None, timeout=timeout)
            data.update(state='completed', message='検査用データの更新確認が完了しました。')
        except SetupCancelled as error:
            data.update(state='cancelled', message=str(error))
        except Exception as error:
            data.update(state='failed', message=str(error) if isinstance(error, SetupError) else
                        '検査用データの更新確認に失敗しました。アプリを開いて状態を確認してください。')
        finally:
            data.update(finished=time.time())
            atomic_write(root / 'update-output.log', backend.last_output.encode())
            atomic_write(root / 'update-state.json', json.dumps(data, ensure_ascii=False).encode())
    return 0 if data['state'] in ('completed', 'cancelled') else 2


class MacAutoUpdateService:
    def __init__(self, root=None, directory=None, run=None, command=None, label=None):
        self.root = Path(root) if root else mac_data_directory()
        self.agent = LaunchAgent(label or 'com.jidaikobo.ClamAVGui.definitions', directory, run)
        self.command = command or helper_command

    def configuration(self):
        return {'ProgramArguments': [*self.command(), 'update', '--support-dir', str(self.root)],
                'ClamAVGuiSupport': str(self.root), 'StartInterval': 7200, 'RunAtLoad': True,
                'ProcessType': 'Background', 'LowPriorityIO': True, 'Umask': 0o077,
                'StandardErrorPath': str(self.root / 'background-errors.log'),
                'ExitTimeOut': 15, 'ThrottleInterval': 60}

    def status(self):
        data = self.agent.read()
        configured = MacSetup(self.root).inspect()
        if data is None:
            return MacAutoUpdateState(False, False, configured.engine and configured.configuration, 12,
                                      '有効にすると、ログイン中はアプリを閉じても約2時間ごとに更新を確認します。')
        if data.get('ClamAVGuiSupport') != str(self.root):
            return MacAutoUpdateState(None, None, False, detail='別の保存先の更新設定があるため、切り替えできません。')
        enabled = self.agent.enabled()
        loaded = self.agent.loaded()
        running = loaded and self.agent.running()
        detail = ''
        manageable = configured.engine and configured.configuration
        if enabled and not loaded:
            detail = '自動更新の設定はありますが登録されていません。スイッチを入れ直してください。'
        if data.get('ProgramArguments') != self.configuration()['ProgramArguments']:
            detail = 'アプリの場所が変わっています。自動更新をオフにしてからオンにして再登録してください。'
        last = read_update(self.root)
        if last and last.get('state') == 'failed':
            detail = last.get('message', '前回の更新確認に失敗しました。')
        return MacAutoUpdateState(enabled, running, manageable, 12, detail)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自動更新の設定が不正です。')
        previous = self.agent.read()
        if previous and previous.get('ClamAVGuiSupport') != str(self.root):
            raise ValueError('別の保存先の自動更新設定は変更できません。')
        if enabled:
            state = MacSetup(self.root).inspect()
            if not state.engine or not state.configuration:
                raise ValueError('ClamAVの準備を完了してから自動更新を有効にしてください。')
            configuration = self.configuration()
            changed = previous is None or any(previous.get(k) != v for k, v in configuration.items())
            if changed:
                if previous is not None and self.agent.running():
                    raise ValueError('更新が完了してから自動更新の設定を変更してください。')
                self.agent.unload()
                self.agent.write(configuration)
            self.agent.enable()
        elif previous is not None:
            self.agent.disable()
            if self.agent.loaded() and self.agent.running():
                self.agent.command('kill', 'SIGTERM', self.agent.target)
                deadline = time.monotonic() + 15
                while self.agent.running() and time.monotonic() < deadline:
                    time.sleep(.1)
                if self.agent.running():
                    raise RuntimeError('自動更新の停止を確認できませんでした。現在の状態を確認してください。')
        state = self.status()
        if state.enabled != enabled or (not enabled and state.running):
            raise RuntimeError('自動更新の変更後の状態を確認できませんでした。')
        return ('自動更新を有効にしました。アプリを閉じても更新を確認します。' if enabled else
                '自動更新を無効にしました。'), state

    def update(self, cancel=None, progress=None, timeout=1800):
        cancel = cancel or threading.Event()
        progress = progress or (lambda message, fraction: None)
        state = self.status()
        if not state.enabled:
            backend = MacSetup(self.root)
            try:
                backend.prepare(cancel, progress, force_update=True)
            finally:
                self.last_output = backend.last_output
            return '検査用データの更新確認が完了しました。'
        before = read_update(self.root)
        target_token = before['token'] if live_update(before) else None
        # kickstart without -k reuses a running job. No second FreshClam is started.
        self.agent.command('kickstart', self.agent.target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = read_update(self.root)
            if data and ((target_token and data['token'] == target_token) or
                         (not target_token and (not before or data['token'] != before['token']))):
                if data.get('state') == 'completed':
                    return data['message']
                if data.get('state') in ('failed', 'cancelled'):
                    raise SetupError(data['message'])
            if cancel.wait(.2):
                raise SetupCancelled('更新の監視を終了しました。自動更新は継続します。')
        raise SetupError('更新確認の完了を確認できませんでした。自動更新は継続します。')
