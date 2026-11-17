"""Scanner independent of the desktop UI; never changes ClamAV configuration."""
from contextlib import contextmanager
import datetime
import json
import uuid
import os
import fcntl
import re
import tempfile
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import sqlite3
import subprocess
from .runtime import track_scan
from .mailboxes import MAILBOX_NOTE, mailbox_findings, mailbox_guidance
from .options import DEFAULTS, supported_options, option_arguments, describe_options, LIMITS, validate_limits, limit_arguments, describe_limits


def engine_status():
    binary = shutil.which('clamscan')
    if not binary:
        return 'ClamAVが見つかりません。OS側への導入が必要です。'
    try:
        result = subprocess.run([binary, '--version'], capture_output=True,
                                text=True, timeout=10)
        return (result.stdout.strip() if result.returncode == 0 else
                'ClamAVの確認に失敗しました。')
    except (OSError, subprocess.TimeoutExpired):
        return 'ClamAVを起動・確認できません。'


class Store:
    def __init__(self, directory=None):
        base = Path(directory) if directory else Path(
            os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))
        ) / 'clamav-gui'
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = base / 'history.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scans '
                       '(started TEXT, target TEXT, status TEXT, output TEXT)')
            if 'profile_id' not in {row[1] for row in db.execute('PRAGMA table_info(scans)')}:
                db.execute('ALTER TABLE scans ADD COLUMN profile_id TEXT')
            columns = {row[1] for row in db.execute('PRAGMA table_info(scans)')}
            for column, kind in [('completed_at', 'TEXT'), ('detected', 'INTEGER')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE scans ADD COLUMN {column} {kind}')
            db.execute('CREATE TABLE IF NOT EXISTS profiles '
                       '(id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS schedules '
                       '(profile_id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS scan_runs '
                       '(token TEXT PRIMARY KEY, profile_id TEXT NOT NULL, pid INTEGER NOT NULL, '
                       'identity TEXT, started TEXT NOT NULL, cancel_requested INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS preferences '
                       '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, started, target, status, output, profile_id=None):
        with self.connect() as db:
            db.execute('INSERT INTO scans (started, target, status, output, profile_id, completed_at, detected) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (started, target, status, output, profile_id,
                        datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
                        int(has_detection(status, output))))

    def recent(self):
        with self.connect() as db:
            return db.execute('SELECT started, target, status, output FROM scans '
                              'ORDER BY rowid DESC LIMIT 100').fetchall()

    def profile_history(self, key, plan):
        with self.connect() as db:
            return db.execute('SELECT started, target, status, output FROM scans '
                              'WHERE profile_id=? OR (profile_id IS NULL AND target=?) '
                              'ORDER BY rowid DESC LIMIT 100', (key, plan.describe())).fetchall()

    def history_entries(self, key, plan):
        with self.connect() as db:
            rows = db.execute('SELECT rowid, started, completed_at, detected, target, status, output '
                              'FROM scans WHERE profile_id=? OR (profile_id IS NULL AND target=?) '
                              'ORDER BY rowid DESC LIMIT 100', (key, plan.describe())).fetchall()
        return [HistoryEntry(*row) for row in rows]

    def scan_options(self):
        with self.connect() as db:
            rows = dict(db.execute('SELECT key, value FROM preferences'))
        settings = dict(DEFAULTS)
        if 'phishing' in rows:
            settings['phishing-sigs'] = settings['phishing-scan-urls'] = json.loads(rows['phishing'])
        if 'scan-options' in rows:
            saved = json.loads(rows['scan-options'])
            if not isinstance(saved, dict) or any(key not in DEFAULTS or type(value) is not bool
                                                  for key, value in saved.items()):
                raise ValueError('検査設定が不正です。環境設定を確認してください。')
            settings.update(saved)
        return settings

    def set_scan_options(self, settings):
        if set(settings) != set(DEFAULTS) or any(type(value) is not bool for value in settings.values()):
            raise ValueError('検査設定が不正です。')
        with self.connect() as db:
            db.execute('INSERT INTO preferences VALUES (?, ?) ON CONFLICT(key) '
                       'DO UPDATE SET value=excluded.value', ('scan-options', json.dumps(settings)))

    def scan_limits(self):
        with self.connect() as db:
            row = db.execute('SELECT value FROM preferences WHERE key=?', ('scan-limits',)).fetchone()
        values = json.loads(row[0]) if row else {key: None for key in LIMITS}
        validate_limits(values)
        return values

    def set_scan_preferences(self, settings, limits):
        if set(settings) != set(DEFAULTS) or any(type(value) is not bool for value in settings.values()):
            raise ValueError('検査設定が不正です。')
        validate_limits(limits)
        with self.connect() as db:
            for key, value in [('scan-options', settings), ('scan-limits', limits)]:
                db.execute('INSERT INTO preferences VALUES (?, ?) ON CONFLICT(key) '
                           'DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def phishing_enabled(self):
        options = self.scan_options()
        return options['phishing-sigs'] and options['phishing-scan-urls']

    def set_phishing(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('有効または無効を指定してください。')
        settings = self.scan_options()
        settings['phishing-sigs'] = settings['phishing-scan-urls'] = enabled
        self.set_scan_options(settings)

    def effective_plan(self, plan):
        return replace(plan, phishing=self.phishing_enabled())

    def profiles(self):
        with self.connect() as db:
            rows = db.execute('SELECT id, name, data FROM profiles ORDER BY rowid').fetchall()
        return [(key, Plan(name, **json.loads(data))) for key, name, data in rows]

    def save_profile(self, plan, key=None):
        validate_profile(plan)
        plan = Plan(plan.label, [os.path.normpath(str(p)) for p in plan.targets],
                    [os.path.normpath(str(p)) for p in plan.excludes], plan.phishing,
                    [os.path.normpath(str(p)) for p in plan.excluded_dirs], plan.requires_admin)
        key = key or str(uuid.uuid4())
        data = json.dumps(dict(targets=[str(p) for p in plan.targets],
                               excludes=[str(p) for p in plan.excludes],
                               excluded_dirs=[str(p) for p in plan.excluded_dirs],
                               phishing=plan.phishing, requires_admin=plan.requires_admin))
        with self.connect() as db:
            db.execute('INSERT INTO profiles VALUES (?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data',
                       (key, plan.label.strip(), data))
        return key

    def duplicate_profile(self, key):
        with self.connect() as db:
            row = db.execute('SELECT name, data FROM profiles WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('検査プランが見つかりません。')
            validate_profile(Plan(row[0], **json.loads(row[1])))
            names = {item[0] for item in db.execute('SELECT name FROM profiles')}
            name = row[0] + '（複製）'
            index = 2
            while name in names:
                name = row[0] + f'（複製{index}）'
                index += 1
            new_key = str(uuid.uuid4())
            db.execute('INSERT INTO profiles VALUES (?, ?, ?)', (new_key, name, row[1]))
            schedule = db.execute('SELECT data FROM schedules WHERE profile_id=?', (key,)).fetchone()
            if schedule:
                data = json.loads(schedule[0])
                data['enabled'] = False
                db.execute('INSERT INTO schedules VALUES (?, ?)', (new_key, json.dumps(data)))
        return new_key

    def delete_profile(self, key):
        with self.connect() as db:
            db.execute('DELETE FROM schedules WHERE profile_id=?', (key,))
            db.execute('DELETE FROM profiles WHERE id=?', (key,))




def warning_signature(line):
    return any(': ' + prefix in line for prefix in (
        'Heuristics.Limits.Exceeded.', 'Heuristics.Encrypted.', 'Heuristics.OLE2.ContainsMacros',
        'Heuristics.Broken.'))


def has_detection(status, output):
    lines = [line for line in output.splitlines() if line.endswith(' FOUND')]
    if lines:
        return any(not warning_signature(line) for line in lines)
    infected = re.search(r'^Infected files:\s*(\d+)\s*$', output, re.M)
    if infected:
        return int(infected[1]) > 0
    return status.startswith('検出あり')


@dataclass
class HistoryEntry:
    id: int
    started: str
    completed_at: str | None
    detected: int | None
    target: str
    status: str
    output: str

    def date_label(self):
        value = self.completed_at
        if not value:
            match = re.search(r'^End Date:\s*(.+)$', self.output, re.M)
            if match:
                value = match[1].strip().replace(':', '-', 2)
        try:
            date = datetime.datetime.fromisoformat(value or self.started)
            text = date.strftime('%Y/%m/%d %H:%M:%S')
        except ValueError:
            text = value or self.started
        return text if value else text + '（開始・旧記録）'

    def is_detected(self):
        return bool(self.detected) if self.detected is not None else has_detection(self.status, self.output)


@dataclass
class Plan:
    label: str
    targets: list
    excludes: list
    phishing: bool = True
    excluded_dirs: list = field(default_factory=list)
    requires_admin: bool = False

    def describe(self, include_policy=True):
        lines = [self.label, '対象:'] + [str(p) for p in self.targets]
        if self.excludes:
            lines += ['除外（ファイルの完全一致）:'] + [str(p) for p in self.excludes]
        if self.excluded_dirs:
            lines += ['除外（フォルダー配下すべて）:'] + [str(p) for p in self.excluded_dirs]
        if include_policy:
            lines += ['フィッシング検査: ' + ('有効' if self.phishing else '無効（既存設定を継承）'),
                  'サイズ上限・暗号化は警告として報告。定義が3日より古ければ停止。',
                  '削除・隔離は行いません。通常ユーザーで検査します。']
        if self.requires_admin:
            lines.append('管理者権限が必要：条件の表示のみ。GUIでの実行は未対応です。')
        return '\n'.join(lines)


def validate_profile(plan):
    if not plan.label.strip():
        raise ValueError('検査プランの名前を入力してください。')
    if len(plan.targets) != 1 or not Path(plan.targets[0]).is_absolute():
        raise ValueError('基本の対象はフォルダー1つを絶対パスで指定してください。')
    root = Path(os.path.normpath(plan.targets[0]))
    if not root.is_dir():
        raise ValueError('基本の対象フォルダーが見つかりません。')
    for item in plan.excludes + plan.excluded_dirs:
        path = Path(os.path.normpath(item))
        if not Path(item).is_absolute() or path == root or root not in path.parents:
            raise ValueError('除外は基本の対象フォルダー内を指定してください。対象全体は除外できません。')




def summarize(code, output, cancelled=False):
    def total(name):
        return sum(int(n) for n in re.findall(r'^' + name + r':\s*(\d+)\s*$', output, re.M))
    found = [line for line in output.splitlines() if line.endswith(' FOUND')]
    limits = [line for line in found if ': Heuristics.Limits.Exceeded.' in line]
    encrypted = [line for line in found if ': Heuristics.Encrypted.' in line]
    warnings = [line for line in found if warning_signature(line)]
    threats = [line for line in found if line not in warnings]
    errors = total('Total errors')
    incomplete = code not in (0, 1) or errors > 0 or 'WARNING:' in output or 'ERROR' in output
    detected = bool(threats) or (total('Infected files') > 0 and not warnings)
    if cancelled:
        title = '検査を中止しました（未検査の対象が残っています）'
    elif incomplete or limits or encrypted:
        title = '検出あり・検査不完了' if detected else '検査不完了'
    elif detected:
        title = '検出あり：詳細を確認してください'
    elif warnings:
        title = '確認が必要です：マクロ・破損などの警告があります'
    elif code == 1:
        title = '確認が必要です：ClamAVが警告を返しました'
    else:
        title = f"検査完了：{total('Scanned files')}ファイルを検査し、脅威は見つかりませんでした"
    details = []
    if limits:
        details.append(f'サイズ・展開などの検査上限に達した対象が{len(limits)}件あります。完全には検査できていません。')
    if encrypted:
        details.append(f'暗号化により内容を確認できない対象が{len(encrypted)}件あります。')
    if threats:
        details.append(f'脅威として報告された対象が{len(threats)}件あります。詳細ログを確認してください。')
    if errors:
        details.append(f'検査エラーが{errors}件あります。対象と理由は詳細ログを確認してください。')
    if 'Permission denied' in output:
        details.append('アクセス権限がないため、読み取れない対象があります。')
    return '\n'.join([title] + details)


def record_scan_failure(store, plan, error, profile_id=None, started=None):
    """Preserve failures that happen before scan() can write its normal result."""
    started = started or datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    reason = str(error).strip() or type(error).__name__
    status = '検査不完了：' + reason
    output = type(error).__name__ + ': ' + reason
    try:
        store.save(started, plan.describe(), status, output, profile_id)
    except Exception as storage_error:
        detail = str(storage_error).strip() or type(storage_error).__name__
        status += '\n検査履歴も保存できませんでした：' + detail
        output += '\n履歴保存エラー：' + detail
    return status, output


def scan(target, store, cancel=None, lock_path=None, profile_id=None):
    plan = target if isinstance(target, Plan) else Plan('フォルダー検査', [Path(target).absolute()], [])
    plan = store.effective_plan(plan)
    cancel = cancel if cancel is not None else threading.Event()
    if plan.requires_admin:
        raise ValueError('この設定は管理者権限が必要です。GUIでの実行にはまだ対応していません。')
    validate_profile(plan)
    binary = shutil.which('clamscan')
    if not binary:
        raise ValueError('ClamAVが見つかりません。')
    settings = store.scan_options()
    supported = supported_options(binary)
    limits = store.scan_limits()
    arguments = option_arguments(settings, supported) + limit_arguments(limits, supported)
    started = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    lock_path = Path(lock_path) if lock_path else Path.home() / '.clamdlog/clamscan.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('別の検査が実行中です。完了後に再実行してください。') from None
        with track_scan(store, profile_id, started) as running:
            command = [binary, '--recursive', '--suppress-ok-results',
                       '--heuristic-alerts=yes', '--alert-exceeds-max=yes',
                       '--fail-if-cvd-older-than=3']
            command += arguments
            command += ['--exclude=^' + re.escape(str(p)) + '$' for p in plan.excludes]
            command += ['--exclude-dir=^' + re.escape(os.path.normpath(str(p))) + r'(/|$)'
                        for p in plan.excluded_dirs]
            command += ['--'] + [str(p) for p in plan.targets]
            # A temporary file avoids pipe deadlocks and unbounded in-memory output.
            with tempfile.TemporaryFile() as log:
                try:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, 'LC_ALL': 'C'})
                    cancelled = False
                    while True:
                        try:
                            code = process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            if cancel.is_set() or running.cancelled():
                                cancelled = True
                                process.terminate()
                                try:
                                    code = process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    code = process.wait()
                                break
                    size = log.seek(0, 2)
                    log.seek(max(0, size - 2 * 1024 * 1024))
                    output = log.read().decode('utf-8', errors='replace')
                    if size > 2 * 1024 * 1024:
                        output = 'WARNING: 出力が大きいため末尾2 MiBのみ保存しています。\n' + output
                    status = summarize(code, output, cancelled)
                except OSError as error:
                    status, output = '検査不完了：ClamAVを実行できませんでした', str(error)
            findings = mailbox_findings(output, plan.targets)
            if findings:
                status += f'\n{MAILBOX_NOTE}（検出された保存ファイル：{len(findings)}件）'
                status += '\n複数のメールをまとめて保存できる形式です。'
                status += '\n保存ファイル自体は削除せず、メールアプリで確認してください。'
                output += '\n\n' + mailbox_guidance(findings)
            output += '\n\n--- この検査で使用した設定 ---\n' + describe_options(settings, supported)
            output += '\n' + describe_limits(limits)
            output += '\n検査上限の警告・ヒューリスティック警告: 有効（固定）\n'
            store.save(started, plan.describe(), status, output, profile_id)
            return status, output
