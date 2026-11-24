"""User systemd timers. Only this application's units are managed."""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from .core import validate_profile

MARKER = '# Managed by ClamAV GUI\n'


@dataclass
class Schedule:
    enabled: bool = False
    frequency: str = 'daily'
    hour: int = 10
    minute: int = 0
    weekday: int = 0
    monthday: int = 1
    interval_minutes: int = 5
    weekdays: list | None = None

    def selected_weekdays(self):
        days = [self.weekday] if self.weekdays is None else self.weekdays
        if not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError('曜日の設定が不正です。')
        if self.frequency == 'weekly' and not days:
            raise ValueError('毎週の検査は曜日を1つ以上選択してください。')
        return sorted(set(days))

    def calendar(self):
        if self.frequency not in ('daily', 'weekly', 'monthly', 'hourly', 'interval'):
            raise ValueError('実行頻度を確認してください。')
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError('時刻を確認してください。')
        if not 0 <= self.weekday <= 6 or not 1 <= self.monthday <= 28:
            raise ValueError('曜日・日付を確認してください。毎月の日付は1〜28日です。')
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 1440:
            raise ValueError('間隔は1〜1440分で指定してください。')
        days = self.selected_weekdays()
        if self.frequency == 'interval':
            return None
        if self.frequency == 'hourly':
            return f'*-*-* *:{self.minute:02}:00'
        time = f'{self.hour:02}:{self.minute:02}:00'
        if self.frequency == 'weekly':
            return ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][day] for day in days) + ' *-*-* ' + time
        if self.frequency == 'monthly':
            return f'*-*-{self.monthday:02} ' + time
        return '*-*-* ' + time

    def describe(self):
        self.calendar()
        if not self.enabled:
            return '定期検査：無効'
        if self.frequency == 'interval':
            return f'定期検査：完了から{self.interval_minutes}分後に繰り返し（初回は登録から{self.interval_minutes}分後）'
        if self.frequency == 'hourly':
            return f'定期検査：毎時{self.minute:02}分（端末の現地時刻）'
        frequency = {'daily': '毎日', 'weekly': '毎週' + '・'.join('月火水木金土日'[day] for day in self.selected_weekdays()) + '曜日',
                     'monthly': f'毎月{self.monthday}日'}[self.frequency]
        return f'定期検査：{frequency} {self.hour:02}:{self.minute:02}（端末の現地時刻）'


def read_schedule(store, key):
    with store.connect() as db:
        row = db.execute('SELECT data FROM schedules WHERE profile_id=?', (key,)).fetchone()
    return Schedule(**json.loads(row[0])) if row else Schedule()


def read_schedules(store):
    with store.connect() as db:
        rows = db.execute('SELECT profile_id,data FROM schedules').fetchall()
    return {key: Schedule(**json.loads(data)) for key, data in rows}


def save_schedule(store, key, schedule):
    schedule.calendar()
    with store.connect() as db:
        db.execute('INSERT INTO schedules VALUES (?, ?) ON CONFLICT(profile_id) '
                   'DO UPDATE SET data=excluded.data', (key, json.dumps(asdict(schedule))))


def quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('改行を含む実行パスには対応していません。')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


class Scheduler:
    def __init__(self, store, unit_dir=None, run=None):
        self.store = store
        self.unit_dir = Path(unit_dir) if unit_dir else Path(
            os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'systemd/user'
        self.run = run or self._run

    @staticmethod
    def _run(*args):
        return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                              text=True, timeout=20, check=True)

    def paths(self, key):
        name = 'clam-gui-' + hashlib.sha256(key.encode()).hexdigest()[:20]
        return self.unit_dir/(name+'.service'), self.unit_dir/(name+'.timer')

    def units(self, key, schedule):
        service, _ = self.paths(key)
        runner = Path(__file__).resolve().parents[1]/'scripts/run_scheduled.py'
        args = ['/usr/bin/python3', str(runner), '--data-dir', str(self.store.path.parent), '--profile', key]
        body = (MARKER + '[Unit]\nDescription=ClamAV GUI scheduled scan\n\n[Service]\n'
                'Type=oneshot\nExecStart=' + ' '.join(quote(arg) for arg in args) +
                '\nUMask=0077\nNice=10\nTimeoutStopSec=10\n')
        schedule.calendar()
        if schedule.frequency == 'interval':
            timing = (f'OnActiveSec={schedule.interval_minutes}min\n'
                      f'OnUnitInactiveSec={schedule.interval_minutes}min\nAccuracySec=1s\n')
        else:
            timing = 'OnCalendar=' + schedule.calendar() + '\nPersistent=true\nAccuracySec=1s\n'
        timer = (MARKER + '[Unit]\nDescription=ClamAV GUI scan timer\n\n[Timer]\n' +
                 timing + 'Unit=' + service.name + '\n\n[Install]\nWantedBy=timers.target\n')
        return body, timer

    @staticmethod
    def write(path, content):
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(content)
                file.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def apply(self, key, schedule):
        plan = dict(self.store.profiles()).get(key)
        if plan is None:
            raise ValueError('検査プランが見つかりません。')
        if schedule.enabled:
            validate_profile(plan)
        if schedule.enabled and plan.requires_admin:
            raise ValueError('管理者権限用の旧設定が残っています。編集画面で「通常ユーザーで検査する」を選んでください。')
        is_watch = plan.kind == 'watch'
        content = self.units(key, schedule)
        paths = self.paths(key)
        previous = {}
        for path in paths:
            if path.is_symlink():
                raise ValueError('予定登録先がシンボリックリンクのため停止しました。')
            previous[path] = path.read_text() if path.exists() else None
            if previous[path] is not None and not previous[path].startswith(MARKER):
                raise ValueError('このアプリが管理していないファイルがあるため停止しました。')
        old = read_schedule(self.store, key)
        if is_watch and all(text is None for text in previous.values()):
            save_schedule(self.store, key, schedule)
            return
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        try:
            if schedule.enabled and not is_watch:
                for path, text in zip(paths, content):
                    self.write(path, text)
                save_schedule(self.store, key, schedule)
                self.run('daemon-reload')
                self.run('enable', '--now', paths[1].name)
                self.run('restart', paths[1].name)
            elif paths[1].exists() or (old.enabled and not is_watch):
                # Disable future starts; a scan already running is left to finish.
                self.run('disable', '--now', paths[1].name)
            save_schedule(self.store, key, schedule)
        except Exception:
            try:
                save_schedule(self.store, key, old)
                self.run('disable', '--now', paths[1].name)
                for path, text in previous.items():
                    if text is None:
                        path.unlink(missing_ok=True)
                    else:
                        self.write(path, text)
                self.run('daemon-reload')
                if old.enabled and previous[paths[1]] is not None:
                    self.run('enable', '--now', paths[1].name)
            except Exception as recovery:
                raise RuntimeError('予定登録に失敗し、復元も完了できませんでした。systemdの状態を確認してください。') from recovery
            raise

    def remove_profile(self, key):
        paths = self.paths(key)
        originals = {}
        for path in paths:
            if path.is_symlink():
                raise ValueError('予定登録先がシンボリックリンクのため停止しました。')
            text = path.read_text() if path.exists() else None
            if text is not None and not text.startswith(MARKER):
                raise ValueError('このアプリが管理していないファイルがあるため停止しました。')
            originals[path] = text
        old = read_schedule(self.store, key)
        if old.enabled or paths[1].exists():
            # A failed stop must leave the profile intact.
            self.apply(key, replace(old, enabled=False))
        try:
            for path, text in originals.items():
                if text is not None:
                    path.unlink()
            if any(text is not None for text in originals.values()):
                self.run('daemon-reload')
            self.store.delete_profile(key)
        except Exception:
            for path, text in originals.items():
                if text is not None:
                    self.write(path, text)
            # Leave the schedule disabled after a failed deletion, rather than restart scans.
            raise RuntimeError('削除を完了できませんでした。検査プランは残しています。予定の状態を確認してください。')
