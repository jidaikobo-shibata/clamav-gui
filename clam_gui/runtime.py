"""Shared, cooperative scan state. Never signals a PID read from disk."""
from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import os
import sqlite3
import uuid
import sys
import threading
from .execution import ScanAlreadyQueued, ScanQueueFull

_cleanup_guard = threading.Lock()
_pending_cleanup = {}


def cleanup_key(store):
    return str(store.path.resolve()), os.getpid()


def retry_cleanup(store, *, wait=True):
    """Retry only tokens completed by this process, with the registered owner."""
    key = cleanup_key(store)
    with _cleanup_guard:
        pending = dict(_pending_cleanup.get(key, {}))
    if not pending:
        return
    with store.connect() as db:
        if not wait:
            db.execute('PRAGMA busy_timeout=0')
        db.execute('BEGIN IMMEDIATE')
        for token, identity in pending.items():
            row = db.execute('SELECT pid,identity FROM scan_runs WHERE token=?', (token,)).fetchone()
            if row is None or row == (os.getpid(), identity):
                db.execute('DELETE FROM scan_run_details WHERE token=?', (token,))
                db.execute('DELETE FROM scan_runs WHERE token=? AND pid=? AND identity IS ?',
                           (token, os.getpid(), identity))
    with _cleanup_guard:
        current = _pending_cleanup.get(key, {})
        for token in pending:
            current.pop(token, None)
        if not current:
            _pending_cleanup.pop(key, None)


def completed_here(store):
    try:
        retry_cleanup(store, wait=False)
    except sqlite3.Error:
        pass  # Keep retry metadata; reads can still work during a write failure.
    with _cleanup_guard:
        return set(_pending_cleanup.get(cleanup_key(store), {}))


def process_identity(pid):
    """The backend distinguishes dead, identified, and unknown processes."""
    from .platforms import get_backend
    return get_backend().process_identity(pid)


@dataclass(frozen=True)
class Activity:
    token: str
    profile_id: str
    started: str
    stopping: bool
    known: bool
    phase: str = 'running'
    lane: str = 'full'

    def describe(self, now=None):
        if not self.known:
            return '実行状態を確認できません'
        if self.stopping:
            return '検査待ちを取り消しています…' if self.phase == 'waiting' else '検査を中止しています…'
        if self.phase == 'waiting':
            return '検査待ち：同じ種類の検査が終わると開始します'
        try:
            elapsed = max(0, int(((now or datetime.datetime.now().astimezone()) -
                                 datetime.datetime.fromisoformat(self.started)).total_seconds()))
            duration = f'{elapsed // 60}分' if elapsed >= 60 else f'{elapsed}秒'
            return '検査中・開始から' + duration
        except (ValueError, TypeError):
            return '検査中'


def activities(store):
    completed = completed_here(store)
    with store.connect() as db:
        rows = db.execute("SELECT r.token,r.profile_id,r.pid,r.identity,r.started,r.cancel_requested,COALESCE(d.phase,'running'),COALESCE(d.lane,'full') FROM scan_runs r LEFT JOIN scan_run_details d ON d.token=r.token").fetchall()
    result = []
    for token, profile_id, pid, identity, started, stopping, phase, lane in rows:
        if token in completed:
            continue
        current = process_identity(pid)
        if current is not None and (not current or (identity is not None and current != identity)):
            continue
        result.append(Activity(token, profile_id, started, bool(stopping), current is not None and identity is not None, phase, lane))
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

    def is_next(self):
        completed = completed_here(self.store)
        if self.token is None:
            return True
        with self.store.connect() as db:
            current = db.execute("SELECT r.rowid,COALESCE(d.lane,'full') FROM scan_runs r LEFT JOIN scan_run_details d ON d.token=r.token WHERE r.token=?", (self.token,)).fetchone()
            if not current:
                return False
            rows = db.execute("SELECT r.token,r.pid,r.identity FROM scan_runs r LEFT JOIN scan_run_details d ON d.token=r.token WHERE COALESCE(d.lane,'full')=? AND r.rowid<?", (current[1],current[0])).fetchall()
        return not any(token not in completed and is_alive(pid, identity) for token, pid, identity in rows)

    def started(self, stamp):
        if self.token:
            with self.store.connect() as db:
                db.execute("UPDATE scan_runs SET started=? WHERE token=?", (stamp, self.token))
                db.execute("UPDATE scan_run_details SET phase='running' WHERE token=?", (self.token,))


def is_alive(pid, identity):
    current = process_identity(pid)
    return current is None or bool(current and (identity is None or current == identity))


@contextmanager
def track_scan(store, profile_id, started, *, phase='running', lane='full', deduplicate=False):
    token = uuid.uuid4().hex if profile_id else None
    if token:
        retry_cleanup(store)
        owner_identity = process_identity(os.getpid())
        with store.connect() as db:
            # Serialize registration so separate GUI/CLI processes cannot both enqueue a plan.
            db.execute('BEGIN IMMEDIATE')
            for old, pid, identity in db.execute('SELECT token,pid,identity FROM scan_runs').fetchall():
                if not is_alive(pid, identity):
                    db.execute('DELETE FROM scan_runs WHERE token=?', (old,))
            db.execute('DELETE FROM scan_run_details WHERE token NOT IN (SELECT token FROM scan_runs)')
            if deduplicate:
                duplicate = db.execute("SELECT 1 FROM scan_runs r LEFT JOIN scan_run_details d ON d.token=r.token WHERE r.profile_id=? AND COALESCE(d.lane,'full')=?", (profile_id,lane)).fetchone()
                if duplicate:
                    raise ScanAlreadyQueued('この検査プランはすでに実行中または検査待ちです。')
                if db.execute("SELECT count(*) FROM scan_runs r LEFT JOIN scan_run_details d ON d.token=r.token WHERE COALESCE(d.lane,'full')=?", (lane,)).fetchone()[0] >= 32:
                    raise ScanQueueFull('同じ種類の実行中・待機中の検査が合計上限32件に達しました。完了後に再度依頼してください。')
            db.execute('INSERT INTO scan_runs (token,profile_id,pid,identity,started,cancel_requested) VALUES (?,?,?,?,?,0)',
                       (token, profile_id, os.getpid(), owner_identity, started))
            db.execute('INSERT INTO scan_run_details VALUES (?,?,?)', (token,phase,lane))
    try:
        yield RunningScan(store, token)
    finally:
        if token:
            # Mark completion before attempting a write that may fail or roll back.
            with _cleanup_guard:
                _pending_cleanup.setdefault(cleanup_key(store), {})[token] = owner_identity
            active_error = sys.exc_info()[0] is not None
            try:
                retry_cleanup(store)
            except sqlite3.Error:
                if not active_error:
                    raise
