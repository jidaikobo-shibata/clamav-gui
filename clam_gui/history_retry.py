"""Bounded main-thread failure histories; never wait for a SQLite lock."""
from collections import deque
from dataclasses import dataclass
import datetime
import time
from .notification_retry import report


@dataclass(eq=False)
class Record:
    values: tuple
    completed: datetime.datetime
    size: int


class HistoryRetry:
    LIMIT = 256
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, store, clock=time.monotonic, changed=lambda:None):
        self.store, self.clock, self.changed = store, clock, changed
        self.rows = deque()
        self.bytes = self.due = self.attempts = 0
        self.error = self.overflow = ''
        self.draining = False

    def save(self, started, target, status, output, profile_id=None):
        values = started, target, status, output, profile_id
        size = sum(len(str(value).encode('utf-8')) for value in values) + 128
        if len(self.rows) >= self.LIMIT or self.bytes + size > self.MAX_BYTES:
            self.overflow = '履歴の保存待ちが上限に達し、一部の履歴を保持できませんでした。'
            report(self.overflow)
            raise OSError(self.overflow)
        row = Record(values, datetime.datetime.now().astimezone(), size)
        self.rows.append(row)
        self.bytes += size
        self.drain()
        if row in self.rows:
            raise OSError('履歴を保持して保存を再試行します：' + self.error)

    def drain(self, *, force=False, notify=True):
        if self.draining or not self.rows or (not force and self.clock() < self.due):
            return
        self.draining = True
        changed = False
        deadline = time.monotonic() + .008
        try:
            for _ in range(4):
                if not self.rows:
                    break
                row = self.rows[0]
                try:
                    self.store.save(*row.values, wait=False, completed_at=row.completed)
                except Exception as error:
                    self.error = str(error)[:256]
                    self.attempts += 1
                    self.due = self.clock() + min(300, 5 * 2**min(self.attempts-1, 6))
                    report('監視の履歴保存を再試行します：' + self.error)
                    break
                self.rows.popleft()
                self.bytes -= row.size
                self.error = ''
                self.due = self.attempts = 0
                changed = True
                if time.monotonic() >= deadline:
                    break
        finally:
            self.draining = False
        if changed and notify:
            try:
                self.changed()
            except Exception as error:
                report('履歴は保存しましたが表示を更新できません：' + str(error)[:256])

    def describe(self):
        details = []
        if self.rows:
            details.append(f'履歴の保存待ち：{len(self.rows)}件（自動で再試行します）')
        if self.overflow:
            details.append(self.overflow)
        return '\n'.join(details)
