"""Request updates from Ubuntu's existing FreshClam service and observe its journal."""
import datetime
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

    def update(self, cancel=None, progress=None, timeout=1800):
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
