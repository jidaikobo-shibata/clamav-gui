"""Bounded enqueue retries; the monitor lease owns this private recovery file."""
import json
import os
from pathlib import Path
import tempfile
import time


def report(message):
    # Reporting a failure must never prevent persistence or resource cleanup.
    try:
        print(message, flush=True)
    except Exception:
        pass


class NotificationRetry:
    LIMIT = 256
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, directory, clock=time.monotonic):
        self.path = Path(directory)/'notification-retry.json'
        self.clock = clock
        self.rows = []
        self.loaded = False
        self.due = self.attempts = 0
        self.error = ''

    @property
    def count(self):
        return sum(row['count'] for row in self.rows)

    def load(self):
        if self.loaded:
            return
        if self.path.is_symlink():
            raise ValueError('通知の退避ファイルがシンボリックリンクです。')
        try:
            with self.path.open('rb') as file:
                raw = file.read(self.MAX_BYTES + 1)
        except FileNotFoundError:
            self.loaded = True
            return
        if len(raw) > self.MAX_BYTES:
            raise ValueError('通知の退避ファイルが上限を超えています。')
        saved = json.loads(raw)
        if not isinstance(saved, dict) or saved.get('version') != 1 or set(saved) != {'version', 'rows'}:
            raise ValueError('通知の退避形式に対応していません。')
        data = saved['rows']
        if not isinstance(data, list) or len(data) > self.LIMIT + 2:
            raise ValueError('通知の退避内容が不正です。')
        for row in data:
            if (not isinstance(row, dict) or set(row) != {'title','body','urgency','count','summary'} or
                not isinstance(row['title'], str) or len(row['title']) > 256 or
                not isinstance(row['body'], str) or len(row['body']) > 8192 or
                row['urgency'] not in ('normal','critical') or type(row['summary']) is not bool or
                type(row['count']) is not int or not 1 <= row['count'] <= 2**63-1):
                raise ValueError('通知の退避内容が不正です。')
        previous = self.rows
        self.rows = []
        for row in data + previous:
            self._add_row(row)
        self.loaded = True

    def _add_row(self, incoming):
        urgency = incoming['urgency']
        if not incoming['summary'] and sum(not row['summary'] for row in self.rows) < self.LIMIT:
            self.rows.append(dict(incoming))
            return
        row = next((row for row in self.rows if row['summary'] and row['urgency'] == urgency), None)
        if row:
            row['count'] += incoming['count']
        else:
            self.rows.append(dict(title='ClamAV 自動検査の通知（まとめ）',body='',urgency=urgency,
                                  count=incoming['count'],summary=True))

    def add(self, notice):
        # Called only by the monitor lease owner, on its GLib thread.
        try:
            self.load()
        except (OSError, ValueError) as error:
            self.error = str(error)[:256]
        urgency = 'critical' if notice.urgency == 'critical' else 'normal'
        self._add_row(dict(title=notice.title[:256],body=notice.body[:8192],urgency=urgency,count=1,summary=False))
        if self.error and self.loaded:
            try:
                self.persist()
            except Exception as error:
                self.error = str(error)[:256]
                report(f'未保存通知{self.count}件の退避に失敗しました：' + self.error)

    def persist(self):
        if not self.loaded:
            raise ValueError('既存の通知退避ファイルを確認できないため上書きしません。')
        if self.path.is_symlink():
            raise ValueError('通知の退避ファイルがシンボリックリンクです。')
        if not self.rows:
            self.path.unlink(missing_ok=True)
            return
        # ensure_ascii=False keeps the bounded string payload below the byte cap.
        data = json.dumps(dict(version=1,rows=self.rows), ensure_ascii=False).encode('utf-8')
        if len(data) > self.MAX_BYTES:
            raise ValueError('通知の退避内容が容量上限を超えています。')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.notification-retry-', delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def drain(self, enqueue, *, force=False):
        if not self.rows or (not force and self.clock() < self.due):
            return
        changed = False
        try:
            self.load()
            for _ in range(8):
                if not self.rows:
                    break
                row = next((row for row in self.rows if row['urgency'] == 'critical'), self.rows[0])
                body = row['body']
                if row['summary']:
                    body = f"未保存の通知が{row['count']}件あります。検査プランのログを確認してください。"
                if row['count'] == 1:
                    enqueue(row['title'], body, row['urgency'])
                else:
                    enqueue(row['title'], body, row['urgency'], count=row['count'])
                self.rows.remove(row)
                changed = True
            self.error = ''
            self.attempts = self.due = 0
        except Exception as error:
            self.error = str(error)[:256]
            self.attempts += 1
            self.due = self.clock() + min(300, 5 * 2**min(self.attempts-1, 6))
            report('通知キューへの保存を再試行します：' + self.error)
        # On failure persist the original rows; on success acknowledge only after
        # the SQLite enqueue commit. A crash between the two may replay a notice.
        if self.rows or changed or self.path.exists():
            try:
                self.persist()
            except Exception as error:
                self.error = str(error)[:256]
                report(f'未保存通知{self.count}件の退避に失敗しました：' + self.error)
