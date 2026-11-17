"""Request updates from Ubuntu's existing FreshClam service and observe its journal."""
import datetime
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import threading
import time

SERVICE = 'clamav-freshclam.service'


def definition_update_label(directory=Path('/var/lib/clamav')):
    """Local database modification time, never the last update-check time."""
    try:
        dates = []
        for pattern in ('*.cvd', '*.cld', '*.cud'):
            for path in Path(directory).glob(pattern):
                if path.is_file():
                    info = path.stat()
                    if info.st_size:
                        dates.append(info.st_mtime)
        if not dates:
            return '定義ファイル更新：未取得または確認できません'
        updated = datetime.datetime.fromtimestamp(max(dates)).astimezone()
        return f'定義ファイル更新：{updated:%Y/%m/%d %H:%M}'
    except (OSError, ValueError, OverflowError):
        return '定義ファイル更新：日時を確認できません'


@dataclass(frozen=True)
class AutoUpdateState:
    enabled: bool | None = None
    running: bool | None = None
    manageable: bool = False
    checks: int | None = None
    detail: str = ''

    def label(self):
        if self.enabled is None:
            return '自動更新：確認できません'
        setting = '有効' if self.enabled else '無効'
        runtime = (('稼働中' if self.enabled else '一時稼働中') if self.running is True else
                   '停止中' if self.running is False else '稼働状態不明')
        return f'自動更新：{setting}（{runtime}）'

    def frequency(self):
        if self.checks:
            hours = 24 / self.checks
            interval = f'約{hours:g}時間ごと' if hours >= 1 else f'約{60 * hours:g}分ごと'
            return f'確認頻度：1日{self.checks}回（{interval}）'
        return '確認頻度：確認できません'


class AutoUpdateService:
    def __init__(self, run=None, config=Path('/etc/clamav/freshclam.conf')):
        self.run = run or subprocess.run
        self.config = Path(config)

    def status(self):
        result = self.run(['/usr/bin/systemctl', 'show', SERVICE,
                           '--property=LoadState,UnitFileState,ActiveState'],
                          capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError('自動更新サービスの状態を確認できません。' + result.stderr.strip())
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        loaded, unit, active = (values.get(key) for key in ('LoadState', 'UnitFileState', 'ActiveState'))
        checks = None
        try:
            for line in self.config.read_text().splitlines():
                match = re.fullmatch(r'Checks\s+(\d+)', line.split('#', 1)[0].strip())
                if match and int(match[1]) > 0:
                    checks = int(match[1])
        except OSError:
            pass
        manageable = loaded == 'loaded' and unit in ('enabled', 'disabled') and active in ('active', 'inactive', 'failed')
        enabled = unit == 'enabled' if unit in ('enabled', 'disabled') else None
        running = active == 'active' if active in ('active', 'inactive', 'failed') else None
        detail = '' if manageable else '未導入・マスク・独自設定などのため、この画面では切り替えできません。'
        return AutoUpdateState(enabled, running, manageable, checks, detail)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自動更新の設定が不正です。')
        if not self.status().manageable:
            raise ValueError('このサービス設定はアプリから変更できません。')
        command = ['/usr/bin/pkexec', '/usr/bin/systemctl',
                   'enable' if enabled else 'disable', '--now', SERVICE]
        result = self.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode in (126, 127):
            return '設定変更をキャンセルしました。', self.status()
        if result.returncode:
            raise ValueError('自動更新の設定を変更できませんでした。' + result.stdout + result.stderr)
        state = self.status()
        if state.enabled != enabled or state.running != enabled:
            raise ValueError('設定変更後の状態が予定と一致しません。現在の状態を確認してください。')
        return ('自動更新を有効にしました。アプリを閉じても更新を確認します。' if enabled else
                '自動更新を無効にしました。他のClamAV連携の定義も自動更新されなくなります。'), state


class UpdateTracker:
    def __init__(self, expected=('daily', 'main', 'bytecode')):
        self.expected = set(expected)
        self.ready = set()
        self.started = False
        self.failed = False

    def feed(self, message):
        if 'ClamAV update process started at' in message or 'Received signal: wake up' in message:
            self.ready.clear()
            self.failed = False
            self.started = True
        if 'ERROR:' in message or re.search(r'(?:HTTP|error|code).*\b(?:403|429)\b', message, re.I) or 'cool-down' in message.lower():
            self.failed = True
        match = re.search(r'\b(daily|main|bytecode)\.(?:cvd|cld) (?:database is up-to-date|is up to date|updated)\b', message)
        if match:
            self.ready.add(match[1])

    def complete(self):
        return self.started and not self.failed and self.expected <= self.ready


class DefinitionUpdater:
    def __init__(self, run=None, database=Path('/var/lib/clamav'),
                 config=Path('/etc/clamav/freshclam.conf'), sleep=None, clock=None,
                 freshclam=Path('/usr/bin/freshclam')):
        self.run = run or subprocess.run
        self.freshclam = Path(freshclam)
        self.database = Path(database)
        self.config = Path(config)
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic

    def command(self, arguments, timeout=30):
        return self.run(arguments, capture_output=True, text=True, timeout=timeout)

    def expected(self):
        text = self.config.read_text()
        directives = [line.split('#', 1)[0].strip() for line in text.splitlines()]
        if any(re.match(r'(?:DatabaseCustomURL|ExtraDatabase|ExcludeDatabase)\s', line) for line in directives):
            raise ValueError('独自の定義取得設定があるため、この画面では完了を判定できません。設定を確認してください。')
        for line in directives:
            if line.startswith('DatabaseDirectory '):
                value = line.split(None, 1)[1].strip('"')
                if Path(value) != self.database:
                    raise ValueError('定義ファイルの保存先が標準と異なるため、この画面では完了を確認できません。')
        expected = {'daily', 'main', 'bytecode'}
        if any(re.fullmatch(r'Bytecode\s+(?:no|false)', line, re.I) for line in directives):
            expected.remove('bytecode')
        return expected

    def journal(self, cursor=None):
        arguments = ['/usr/bin/journalctl', '--unit=' + SERVICE, '--output=json', '--no-pager', '--lines=1000']
        if cursor:
            arguments.append('--after-cursor=' + cursor)
        result = self.command(arguments)
        if result.returncode or any(text in result.stderr.lower() for text in ('not seeing messages', 'permission denied', 'no journal files')):
            raise ValueError('更新ログを読み取れません。取得・更新の完了を確認できません。')
        rows = []
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get('MESSAGE'), str) and isinstance(row.get('__CURSOR'), str):
                rows.append(row)
        return rows

    def files_present(self, expected):
        return all(any(path.is_file() and path.stat().st_size > 0
                       for path in (self.database/(name+'.cvd'), self.database/(name+'.cld')))
                   for name in expected)

    def update(self, cancel=None, progress=None, timeout=1800, restore_stopped=False):
        self._temporary_started = False
        temporary_stopped = False
        try:
            result, log = self._update(cancel, progress, timeout)
        finally:
            if restore_stopped and self._temporary_started:
                current = AutoUpdateService(self.run, self.config).status()
                if current.enabled is None or current.running is None:
                    raise ValueError('一時起動したサービスの状態を確認できません。自動更新の設定を確認してください。')
                if current.enabled is False and current.running:
                    stopped = self.command(['/usr/bin/pkexec', '/usr/bin/systemctl', 'stop', SERVICE], timeout=180)
                    if stopped.returncode or AutoUpdateService(self.run, self.config).status().running is not False:
                        raise ValueError('一時起動した更新サービスを停止できませんでした。自動更新の現在の状態を確認してください。')
                    temporary_stopped = True
        if temporary_stopped:
            result = result.replace('サービスは停止していません。', '')
            if '監視を終了' in result:
                result = '監視を終了しました。'
            result += '\n一時起動した更新サービスは停止しました。自動更新は無効のままです。'
        return result, log

    def _update(self, cancel=None, progress=None, timeout=1800):
        cancel = cancel or threading.Event()
        progress = progress or (lambda text: None)
        expected = self.expected()
        if not self.freshclam.exists():
            raise ValueError('FreshClamが見つかりません。先にClamAVをインストールしてください。')
        barrier = int(datetime.datetime.now().timestamp() * 1_000_000)
        initial = self.journal()
        cursor = initial[-1]['__CURSOR'] if initial else None
        state = self.command(['/usr/bin/systemctl', 'is-active', SERVICE])
        if state.returncode not in (0, 3):
            raise ValueError('更新サービスの状態を確認できません。')
        active = state.returncode == 0
        tracker = UpdateTracker(expected)
        for row in initial:
            tracker.feed(row['MESSAGE'])
        pending = active and tracker.started and not tracker.complete() and not tracker.failed
        if cancel.is_set():
            return '監視を終了しました。更新サービスは停止していません。', ''
        if pending:
            log = '自動更新サービスが取得中のため、その処理を監視します。\n'
        else:
            tracker = UpdateTracker(expected)
            if active:
                command = ['/usr/bin/pkexec', '/usr/bin/systemctl', 'kill', '--kill-whom=main', '--signal=SIGUSR1', SERVICE]
            else:
                command = ['/usr/bin/pkexec', '/usr/bin/systemctl', 'start', SERVICE]
            result = self.command(command, timeout=180)
            if result.returncode in (126, 127):
                return '更新依頼をキャンセルしました。', result.stderr
            if result.returncode:
                raise ValueError('更新を依頼できませんでした。\n' + result.stdout + result.stderr)
            self._temporary_started = not active
            log = '更新サービスへ取得・更新を依頼しました。\n'
        progress(log)
        deadline = self.clock() + timeout
        # A journal with no existing rows must use a time barrier, not replay historical entries.
        initial_cursors = {row['__CURSOR'] for row in initial}
        seen = set(initial_cursors)
        while self.clock() < deadline:
            if cancel.is_set():
                return '監視を終了しました。更新サービスはバックグラウンドで継続します。', log
            rows = self.journal(cursor)
            for row in rows:
                cursor = row['__CURSOR']
                if cursor in seen:
                    continue
                seen.add(cursor)
                if not initial:
                    try:
                        if int(row['__REALTIME_TIMESTAMP']) < barrier:
                            continue
                    except (KeyError, ValueError):
                        continue
                message = row['MESSAGE']
                tracker.feed(message)
                log = (log + message + '\n')[-2 * 1024 * 1024:]
            progress(log)
            if tracker.failed:
                return '定義の取得・更新に失敗しました。詳細を確認してください。自動で再試行はしません。', log
            if tracker.complete():
                if not self.files_present(expected):
                    return '更新ログは完了していますが、必要な定義ファイルを確認できません。', log
                return '定義ファイルの取得・更新を確認しました。', log
            service = self.command(['/usr/bin/systemctl', 'is-active', SERVICE])
            if service.returncode != 0:
                return '更新サービスが停止しています。定義の取得完了は確認できませんでした。', log + service.stdout + service.stderr
            self.sleep(1)
        return '完了を確認するまでの待機時間を超えました。サービスは停止していません。ログを再確認してください。', log
