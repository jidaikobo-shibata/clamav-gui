"""Scanner independent of the desktop UI; never changes ClamAV configuration."""
from contextlib import contextmanager
import datetime
import json
import uuid
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import sqlite3
import subprocess
from .retention import DEFAULTS as AUTOMATION_DEFAULTS, validate as validate_automation, prune, initialize as initialize_retention
from .execution import ScanBusyError, ScanAlreadyQueued, ScanFilesChanged, lane_for, lock_path as default_lock_path
from .runtime import track_scan
from .platforms import get_backend
from .settings import Preferences
from .scan_output import ScanOutput
from .results import ResultKind, ScanStatus, assess, has_detection, warning_signature, summarize, output_was_truncated, detection_uncertain
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
        if directory is not None:
            base = Path(directory)
        else:
            data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
            base = data / 'clamav-gui'
            legacy = data / 'clam-gui'
            # Preserve existing installations without copying or replacing data.
            if not (base / 'history.sqlite3').exists() and (legacy / 'history.sqlite3').is_file():
                base = legacy
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = base / 'history.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scans '
                       '(started TEXT, target TEXT, status TEXT, output TEXT)')
            if 'profile_id' not in {row[1] for row in db.execute('PRAGMA table_info(scans)')}:
                db.execute('ALTER TABLE scans ADD COLUMN profile_id TEXT')
            columns = {row[1] for row in db.execute('PRAGMA table_info(scans)')}
            for column, kind in [('completed_at', 'TEXT'), ('detected', 'INTEGER'), ('log_bytes', 'INTEGER'), ('result_kind', 'TEXT')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE scans ADD COLUMN {column} {kind}')
            db.execute('CREATE TABLE IF NOT EXISTS profiles '
                       '(id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS schedules '
                       '(profile_id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS scan_runs '
                       '(token TEXT PRIMARY KEY, profile_id TEXT NOT NULL, pid INTEGER NOT NULL, '
                       'identity TEXT, started TEXT NOT NULL, cancel_requested INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS scan_run_details '
                       '(token TEXT PRIMARY KEY, phase TEXT NOT NULL, lane TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS preferences '
                       '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS scans_profile ON scans(profile_id)')
            db.execute('CREATE INDEX IF NOT EXISTS scans_legacy_target ON scans(target) WHERE profile_id IS NULL')
        with self.connect() as db:
            initialize_retention(db)
        self.path.chmod(0o600)
        self.preferences = Preferences(self.connect)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, started, target, status, output, profile_id=None, *, wait=True, completed_at=None):
        result = assess(status, output)
        encoded = output.encode('utf-8')
        if len(encoded) > 2 * 1024 * 1024:
            marker, separator = '出力が大きいため先頭と末尾のみ保存しています。\n', '\n…\n'
            budget = 2*1024*1024 - len((marker + separator).encode('utf-8'))
            output = (marker + encoded[:384*1024].decode('utf-8', errors='ignore') + separator +
                      encoded[-(budget-384*1024):].decode('utf-8', errors='ignore'))
        size = len((target + status + output).encode('utf-8')) + 128
        completed = completed_at or datetime.datetime.now().astimezone()
        with self.connect() as db:
            if not wait:
                db.execute('PRAGMA busy_timeout=0')
            policy = self.automation_settings(db=db)
            cursor = db.execute('INSERT INTO scans (started, target, status, output, profile_id, completed_at, detected, log_bytes, result_kind) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (started, target, status, output, profile_id,
                        completed.isoformat(timespec='seconds'),
                        int(result.detected), size, result.kind.value))
            db.execute('INSERT INTO retention_info VALUES (?,?,?,?)',
                       (cursor.lastrowid, completed.replace(microsecond=0).timestamp(), int(not result.clean), size))
            prune(db, policy)

    def automation_settings(self, db=None):
        values = dict(AUTOMATION_DEFAULTS)
        values.update(self.preferences.get('automation', {}, db=db))
        validate_automation(values)
        return values

    def set_automation_settings(self, values):
        validate_automation(values)
        with self.connect() as db:
            self.preferences.set('automation', values, db)
            return prune(db, values)

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
            rows = db.execute('SELECT rowid, started, completed_at, detected, target, status, output, result_kind '
                              'FROM scans WHERE profile_id=? OR (profile_id IS NULL AND target=?) '
                              'ORDER BY rowid DESC LIMIT 100', (key, plan.describe())).fetchall()
        return [HistoryEntry(*row) for row in rows]

    def history_headers(self, key, plan):
        # Fetch at most 100 IDs from each indexed branch, then only small fields.
        with self.connect() as db:
            modern = db.execute('SELECT rowid FROM scans WHERE profile_id=? ORDER BY rowid DESC LIMIT 100', (key,)).fetchall()
            legacy = db.execute('SELECT rowid FROM scans WHERE profile_id IS NULL AND target=? ORDER BY rowid DESC LIMIT 100', (plan.describe(),)).fetchall()
            ids = sorted({row[0] for row in modern + legacy}, reverse=True)[:100]
            if not ids:
                return []
            rows = db.execute("SELECT rowid,started,completed_at,detected,'',status,'',result_kind,"
                              "CASE WHEN COALESCE(detected,0)=0 THEN substr(CAST(output AS BLOB),1,256) ELSE X'' END "
                              "FROM scans WHERE rowid IN ("+
                              ','.join('?' for _ in ids)+') ORDER BY rowid DESC', ids).fetchall()
            result = []
            for row in rows:
                entry = HistoryEntry(*row[:8])
                if entry.completed_at is None or entry.detected is None:
                    # Old logs need one-at-a-time decoding; never retain all their bodies.
                    body = db.execute('SELECT output FROM scans WHERE rowid=?', (entry.id,)).fetchone()
                    full = replace(entry, output=body[0] if body else '')
                    entry = replace(entry, completed_at=full.completion_timestamp(), detected=int(full.is_detected()),
                                    uncertain=full.is_uncertain())
                elif not entry.is_detected():
                    prefix = (row[8] or b'').decode('utf-8', errors='replace')
                    probe = prefix
                    if output_was_truncated(prefix):
                        # Only a small head/tail probe is retained for modern rows;
                        # do not load all log bodies just to render the history list.
                        tail = db.execute('SELECT substr(CAST(output AS BLOB),-16384), '
                                          'substr(CAST(output AS BLOB),instr(CAST(output AS BLOB),?),2048) '
                                          'FROM scans WHERE rowid=?',
                                          (b'\n----------- SCAN SUMMARY -----------', entry.id)).fetchone()
                        probe += '\n' + (tail[0] or b'').decode('utf-8', errors='replace')
                        probe += '\n' + (tail[1] or b'').decode('utf-8', errors='replace')
                    entry = replace(entry, uncertain=detection_uncertain(probe, entry.status))
                result.append(entry)
            return result

    def history_entry(self, ident, key, plan):
        with self.connect() as db:
            row = db.execute('SELECT rowid,started,completed_at,detected,target,status,output,result_kind '
                             'FROM scans WHERE rowid=? AND (profile_id=? OR (profile_id IS NULL AND target=?))',
                             (ident, key, plan.describe())).fetchone()
        return HistoryEntry(*row) if row else None

    def scan_options(self):
        rows = self.preferences.get_many(['phishing', 'scan-options'])
        settings = dict(DEFAULTS)
        if 'phishing' in rows:
            settings['phishing-sigs'] = settings['phishing-scan-urls'] = rows['phishing']
        if 'scan-options' in rows:
            saved = rows['scan-options']
            if not isinstance(saved, dict) or any(key not in DEFAULTS or type(value) is not bool
                                                  for key, value in saved.items()):
                raise ValueError('検査設定が不正です。環境設定を確認してください。')
            settings.update(saved)
        return settings

    def set_scan_options(self, settings):
        if set(settings) != set(DEFAULTS) or any(type(value) is not bool for value in settings.values()):
            raise ValueError('検査設定が不正です。')
        self.preferences.set('scan-options', settings)

    def scan_limits(self):
        values = self.preferences.get('scan-limits', {key: None for key in LIMITS})
        validate_limits(values)
        return values

    def set_scan_preferences(self, settings, limits, automation=None):
        if set(settings) != set(DEFAULTS) or any(type(value) is not bool for value in settings.values()):
            raise ValueError('検査設定が不正です。')
        validate_limits(limits)
        with self.connect() as db:
            entries = [('scan-options', settings), ('scan-limits', limits)]
            if automation is not None:
                validate_automation(automation)
                entries.append(('automation', automation))
            self.preferences.set_many(dict(entries), db)
            if automation is not None:
                prune(db, automation)

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

    def display_profiles(self, include_media=False):
        from .plan_order import reconcile, PREFERENCE, MEDIA_ID
        from .automation import media_plan
        with self.connect() as db:
            rows = db.execute('SELECT id, name, data FROM profiles ORDER BY rowid').fetchall()
            plans = [(key, Plan(name, **json.loads(data))) for key, name, data in rows]
            keys = [key for key, _ in plans] + [MEDIA_ID]
            order = reconcile(self.preferences.get(PREFERENCE, [], db=db), keys)
        if include_media:
            plans.append((MEDIA_ID, media_plan()))
        rank = {key: index for index, key in enumerate(order)}
        return sorted(plans, key=lambda item: rank[item[0]])

    def move_display_profile(self, key, anchor, *, after=False):
        from .plan_order import reconcile, PREFERENCE, MEDIA_ID
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            keys = [row[0] for row in db.execute('SELECT id FROM profiles ORDER BY rowid')] + [MEDIA_ID]
            if key not in keys or anchor not in keys:
                raise ValueError('検査プランが見つかりません。一覧を開き直してください。')
            if key == anchor:
                return
            order = reconcile(self.preferences.get(PREFERENCE, [], db=db), keys)
            order.remove(key)
            order.insert(order.index(anchor) + int(after), key)
            self.preferences.set(PREFERENCE, order, db=db)

    def save_profile(self, plan, key=None):
        validate_profile(plan)
        if plan.kind == 'watch':
            from .watch_scope import safe_watch_plan
            safe_watch_plan(plan, self.path.parent)
        if plan.kind == 'media' or key == 'external-media':
            raise ValueError('外部メディア検査は環境設定で管理してください。')
        plan = replace(plan, targets=[os.path.normpath(str(p)) for p in plan.targets],
                       excludes=[os.path.normpath(str(p)) for p in plan.excludes],
                       excluded_dirs=[os.path.normpath(str(p)) for p in plan.excluded_dirs])
        key = key or str(uuid.uuid4())
        data = json.dumps(dict(targets=[str(p) for p in plan.targets],
                               excludes=[str(p) for p in plan.excludes],
                               excluded_dirs=[str(p) for p in plan.excluded_dirs],
                               phishing=plan.phishing, requires_admin=plan.requires_admin,
                               kind=plan.kind, watch_recursive=plan.watch_recursive))
        with self.connect() as db:
            db.execute('INSERT INTO profiles VALUES (?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data',
                       (key, plan.label.strip(), data))
        return key

    def edit_excluded_folders(self, key, folders, removals, *, expected_target, threats=()):
        """Apply only explicit folder additions/removals to the current plan."""
        from .plan_exclusions import exclusion_state
        if key == 'external-media':
            raise ValueError('外部メディア検査はここでは変更できません。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT name,data FROM profiles WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('検査プランは削除されています。')
            data = json.loads(row[1])
            plan = Plan(row[0], **data)
            if tuple(plan.targets) != tuple(expected_target):
                raise ValueError('対象フォルダーが変更されています。候補画面を開き直してください。')
            remove_set = {os.path.normpath(str(item)) for item in removals}
            removed = [item for item in plan.excluded_dirs if os.path.normpath(str(item)) in remove_set]
            plan = replace(plan, excluded_dirs=[item for item in plan.excluded_dirs if item not in removed])
            added = []
            for folder in dict.fromkeys(os.path.normpath(str(item)) for item in folders):
                state = exclusion_state(plan, folder, threats)
                if state == 'すでに除外されています。':
                    continue
                if state:
                    raise ValueError(state)
                added.append(folder)
                plan = replace(plan, excluded_dirs=[*plan.excluded_dirs, folder])
            validate_profile(plan)
            if plan.kind == 'watch':
                from .watch_scope import safe_watch_plan
                safe_watch_plan(plan, self.path.parent)
            data['excluded_dirs'] = plan.excluded_dirs
            db.execute('UPDATE profiles SET data=? WHERE id=?', (json.dumps(data), key))
        return added, removed

    def add_excluded_folders(self, key, folders, *, expected_target):
        added, _ = self.edit_excluded_folders(key, folders, [], expected_target=expected_target)
        return added

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






@dataclass
class HistoryEntry:
    id: int
    started: str
    completed_at: str | None
    detected: int | None
    target: str
    status: str
    output: str
    result_kind: str | None = None
    uncertain: bool | None = None

    def completion_timestamp(self):
        value = self.completed_at
        if not value:
            match = re.search(r'^End Date:\s*(.+)$', self.output, re.M)
            if match:
                value = match[1].strip().replace(':', '-', 2)
        return value

    def date_label(self):
        value = self.completion_timestamp()
        try:
            date = datetime.datetime.fromisoformat(value or self.started)
            text = date.strftime('%Y/%m/%d %H:%M:%S')
        except ValueError:
            text = value or self.started
        return text if value else text + '（開始・旧記録）'

    def is_detected(self):
        return bool(self.detected) if self.detected is not None else has_detection(self.status, self.output)

    def is_uncertain(self):
        if self.is_detected():
            return False
        return self.uncertain if self.uncertain is not None else detection_uncertain(self.output, self.status)

    def attention_label(self):
        if self.is_detected():
            return '検出あり'
        return '要確認・内訳不明' if self.is_uncertain() else ''

    def attention_notice(self):
        if not self.is_uncertain():
            return ''
        return ('要確認・内訳不明\nClamAVは検出情報を報告していますが、ログの一部が省略されているため、'
                '脅威の有無や内訳を確認できません。「検出なし」とは判断できません。'
                '検出明細を保持できる状態で再検査してください。\n\n--- 保存された記録 ---\n')


@dataclass
class Plan:
    label: str
    targets: list
    excludes: list
    phishing: bool = True
    excluded_dirs: list = field(default_factory=list)
    requires_admin: bool = False
    kind: str = 'scheduled'
    watch_recursive: bool = True

    def describe(self, include_policy=True):
        lines = [self.label, '対象:'] + [str(p) for p in self.targets]
        if self.kind == 'watch':
            lines += ['検査方法：フォルダー監視']
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
    if plan.kind not in ('scheduled', 'watch', 'media') or type(plan.watch_recursive) is not bool:
        raise ValueError('検査プランの種類・監視範囲が不正です。')
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






def record_scan_failure(store, plan, error, profile_id=None, started=None, *, save=None):
    """Preserve failures that happen before scan() can write its normal result."""
    started = started or datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    reason = str(error).strip() or type(error).__name__
    status = ScanStatus('検査不完了：' + reason, ResultKind.INCOMPLETE)
    output = type(error).__name__ + ': ' + reason
    try:
        (save or store.save)(started, plan.describe(), status, output, profile_id)
    except Exception as storage_error:
        detail = str(storage_error).strip() or type(storage_error).__name__
        status += '\n検査履歴も保存できませんでした：' + detail
        output += '\n履歴保存エラー：' + detail
    return status, output


def storage_warning(status, output, error):
    result = assess(status, output)
    detail = '検査履歴を保存できませんでした：' + str(error)
    kind = ResultKind.CANCELLED if result.kind == ResultKind.CANCELLED else ResultKind.INCOMPLETE
    title = ('検出あり・履歴保存失敗' if result.detected else
             '検査中止・履歴保存失敗' if kind == ResultKind.CANCELLED else
             '検査不完了：履歴を保存できませんでした')
    return (ScanStatus(title + '\n' + str(status) + '\n' + detail, kind, result.detected,
                       started_at=getattr(status, 'started_at', None)),
            output + '\n履歴保存エラー：' + str(error))


def persist_result(store, plan, started, status, output, profile_id):
    try:
        store.save(started, plan.describe(), status, output, profile_id)
    except Exception as error:
        return storage_warning(status, output, error)
    return status, output


def scan(target, store, cancel=None, lock_path=None, profile_id=None, files=None, available=None, wait=False, can_start=None):
    plan = target if isinstance(target, Plan) else Plan('フォルダー検査', [Path(target).absolute()], [])
    plan = store.effective_plan(plan)
    cancel = cancel if cancel is not None else threading.Event()
    if plan.requires_admin:
        raise ValueError('この設定は管理者権限が必要です。GUIでの実行にはまだ対応していません。')
    if available is not None and not available():
        raise ValueError('検査対象のメディアが取り外されました。')
    validate_profile(plan)
    if files is not None:
        from .automation import allowed_file
        if not files or len(files) > 50:
            raise ValueError('監視による検査ファイルは1〜50件で指定してください。')
        if any(not allowed_file(plan, path) for path in files):
            raise ScanFilesChanged('監視対象のファイルが消失・変更されたか、検査範囲外です。')
    binary = shutil.which('clamscan')
    if not binary:
        raise ValueError('ClamAVが見つかりません。')
    started = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    lane = lane_for(plan, files)
    lock_path = Path(lock_path) if lock_path else default_lock_path(lane)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = get_backend().lock(lock_path)
    result_ready = False
    try:
        with track_scan(store, profile_id, started, phase='waiting' if wait else 'running', lane=lane,
                        deduplicate=True) as running:
            while True:
                reason = None
                if wait and (cancel.is_set() or running.cancelled()):
                    reason = '検査待ちを取り消しました。検査は実行していません。'
                elif available is not None and not available():
                    reason = '検査対象のメディアが取り外されました。'
                elif can_start is not None and not can_start():
                    reason = '予定が無効になったかプランが変更されたため、検査待ちを取り消しました。'
                if reason:
                    status = ScanStatus(reason, ResultKind.CANCELLED, started_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
                    result_ready = True
                    status, output = persist_result(store, plan, started, status, reason, profile_id)
                    return status, output
                if (not wait or running.is_next()) and lock.acquire():
                    break
                if not wait:
                    raise ScanBusyError('別の検査が実行中です。') from None
                cancel.wait(.5)
            # Recheck the target after a potentially long wait, before starting ClamAV.
            validate_profile(plan)
            started = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
            running.started(started)
            settings = store.scan_options()
            supported = supported_options(binary)
            limits = store.scan_limits()
            arguments = option_arguments(settings, supported) + limit_arguments(limits, supported)
            command = [binary, '--recursive', '--suppress-ok-results',
                       '--follow-file-symlinks=0', '--follow-dir-symlinks=0',
                       '--heuristic-alerts=yes', '--alert-exceeds-max=yes',
                       '--fail-if-cvd-older-than=3']
            if plan.kind == 'media':
                command.append('--cross-fs=no')
            command += arguments
            command += ['--exclude=^' + re.escape(str(p)) + '$' for p in plan.excludes]
            command += ['--exclude-dir=^' + re.escape(os.path.normpath(str(p))) + r'(/|$)'
                        for p in plan.excluded_dirs]
            command += ['--'] + [str(p) for p in (files if files is not None else plan.targets)]
            # Drain the pipe while scanning; neither disk output nor memory grows unbounded.
            with ScanOutput() as log:
                process = None
                try:
                    process = get_backend().spawn_scanner(command, lock=lock, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, 'LC_ALL': 'C'})
                    cancelled = False
                    removed = False
                    while True:
                        try:
                            code = process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            if cancel.is_set() or running.cancelled() or (available is not None and not available()):
                                removed = available is not None and not available()
                                cancelled = True
                                process.terminate()
                                try:
                                    code = process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    code = process.wait()
                                break
                    output = log.finish()
                    status = summarize(code, output, cancelled)
                    if log.detected and not status.assessment.detected:
                        status = ScanStatus(str(status), status.assessment.kind, True)
                        status += '\n脅威の検出がありました。省略された出力があるため詳細をすべて表示できません。'
                    if removed or (available is not None and not available()):
                        status = ScanStatus('検査不完了：検査対象のメディアが取り外されました。', ResultKind.INCOMPLETE, has_detection(status, output))
                    if files is not None:
                        output += '\n\n--- 監視による検査ファイル ---\n' + '\n'.join(map(str, files))
                except Exception as error:
                    if process is not None:
                        try:
                            if process.poll() is None:
                                process.terminate()
                                try:
                                    process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait()
                        except OSError:
                            pass
                    output = log.finish() + '\nClamAVの実行エラー：' + str(error)
                    status = ScanStatus('検査不完了：ClamAVを実行できませんでした', ResultKind.INCOMPLETE, log.detected)
            findings = mailbox_findings(output, plan.targets)
            if findings:
                status += f'\n{MAILBOX_NOTE}（検出された保存ファイル：{len(findings)}件）'
                status += '\n複数のメールをまとめて保存できる形式です。'
                status += '\n保存ファイル自体は削除せず、メールアプリで確認してください。'
                output += '\n\n' + mailbox_guidance(findings)
            output += '\n\n--- この検査で使用した設定 ---\n' + describe_options(settings, supported)
            output += '\n' + describe_limits(limits)
            output += '\n検査上限の警告・ヒューリスティック警告: 有効（固定）\n'
            result = assess(status, output)
            status = ScanStatus(str(status), result.kind, result.detected, started_at=started)
            result_ready = True
            status, output = persist_result(store, plan, started, status, output, profile_id)
            return status, output
    except sqlite3.Error as error:
        if not result_ready:
            raise
        return storage_warning(status, output, error)
    finally:
        lock.close()
