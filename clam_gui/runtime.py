"""Shared, cooperative scan state. Never signals a PID read from disk."""
from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import os
import sys
import sqlite3
from pathlib import Path
import uuid


def process_identity(pid):
    """Linux boot/start identity prevents stale records from matching a reused PID."""
    if type(pid) is not int or not 1 <= pid <= 2147483647:
        return None
    if sys.platform == 'darwin':
        from .macos_process import process_identity as mac_identity
        return mac_identity(pid)
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rindex(')') + 2:].split()
        if fields[0] in ('Z', 'X'):
            return ''
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    except FileNotFoundError:
        return '' if Path('/proc').is_dir() else None
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class Activity:
    token: str
    profile_id: str
    started: str
    stopping: bool
    known: bool

    def describe(self, now=None):
        if not self.known:
            return '実行状態を確認できません'
        if self.stopping:
            return '検査を中止しています…'
        try:
            elapsed = max(0, int(((now or datetime.datetime.now().astimezone()) -
                                 datetime.datetime.fromisoformat(self.started)).total_seconds()))
            duration = f'{elapsed // 60}分' if elapsed >= 60 else f'{elapsed}秒'
            return '検査中・開始から' + duration
        except (ValueError, TypeError):
            return '検査中'


def activities(store):
    with store.connect() as db:
        rows = db.execute('SELECT token, profile_id, pid, identity, started, cancel_requested FROM scan_runs').fetchall()
    result = []
    for token, profile_id, pid, identity, started, stopping in rows:
        current = process_identity(pid)
        if current is not None and (not current or (identity is not None and current != identity)):
            continue
        result.append(Activity(token, profile_id, started, bool(stopping), current is not None and identity is not None))
    return result


def request_stop(store, token):
    running = next((a for a in activities(store) if a.token == token and a.known), None)
    if not running:
        return False
    with store.connect() as db:
        return bool(db.execute('UPDATE scan_runs SET cancel_requested=1 WHERE token=?', (token,)).rowcount)


class RunningScan:
    def __init__(self, store, token):
        self.store, self.token = store, token

    def cancelled(self):
        if self.token is None:
            return False
        try:
            with self.store.connect() as db:
                row = db.execute('SELECT cancel_requested FROM scan_runs WHERE token=?', (self.token,)).fetchone()
            return bool(row and row[0])
        except sqlite3.Error:
            return False


@contextmanager
def track_scan(store, profile_id, started):
    token = uuid.uuid4().hex if profile_id else None
    if token:
        with store.connect() as db:
            # Discard proven-dead records without treating them as live scans.
            for old, _, pid, identity in db.execute('SELECT token, profile_id, pid, identity FROM scan_runs').fetchall():
                current = process_identity(pid)
                if current is not None and (not current or (identity is not None and current != identity)):
                    db.execute('DELETE FROM scan_runs WHERE token=?', (old,))
            db.execute('INSERT INTO scan_runs VALUES (?, ?, ?, ?, ?, 0)',
                       (token, profile_id, os.getpid(), process_identity(os.getpid()), started))
    try:
        yield RunningScan(store, token)
    finally:
        if token:
            with store.connect() as db:
                db.execute('DELETE FROM scan_runs WHERE token=?', (token,))
