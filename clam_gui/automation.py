"""Bounded queues and cooperative jobs for foreground-only automatic scans."""
from .jobs import execute_scan
from .execution import ScanAlreadyQueued
from .results import assess
from dataclasses import dataclass
import datetime
import os
from pathlib import Path
import stat
import threading
import time

MEDIA_ID = 'external-media'
TEMP_SUFFIXES = ('.part', '.crdownload', '.download', '.tmp')
SIGNATURE_VERSION = 2


def media_plan():
    from .core import Plan
    return Plan('外部メディア検査', ['/'], [], kind='media')


def signature(path):
    try:
        info = Path(path).lstat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


def allowed_path(plan, path):
    """Exclude symlink components and escapes, including directories used by monitors."""
    root = Path(os.path.abspath(plan.targets[0]))
    path = Path(os.path.abspath(path))
    if path != root and root not in path.parents:
        return False
    try:
        if root.is_symlink() or path.is_symlink():
            return False
        resolved_root, resolved = root.resolve(strict=True), path.resolve(strict=True)
        if resolved != resolved_root and resolved_root not in resolved.parents:
            return False
        current = path
        while current != root:
            if current.is_symlink():
                return False
            current = current.parent
        if str(path) in map(str, plan.excludes):
            return False
        return not any(path == Path(p) or Path(p) in path.parents for p in plan.excluded_dirs)
    except (OSError, ValueError, RuntimeError):
        return False


def allowed_file(plan, path):
    if plan.kind == 'watch' and not plan.watch_recursive and Path(os.path.abspath(path)).parent != Path(os.path.abspath(plan.targets[0])):
        return False
    return allowed_path(plan, path) and signature(path) is not None


@dataclass
class Pending:
    path: str
    signature: tuple
    due: float


class WatchQueue:
    def __init__(self, clock=time.monotonic, quiet=3, limit=1000):
        self.clock, self.quiet, self.limit = clock, quiet, limit
        self.pending = {}
        self.completed = {}

    def put(self, key, plan, path, *, changed=False):
        path = str(Path(os.path.abspath(path)))
        stamp = signature(path)
        if not stamp or path.lower().endswith(TEMP_SUFFIXES) or not allowed_file(plan, path):
            return True
        ident = key, path
        if changed:
            self.completed.pop(ident, None)
        if self.completed.get(ident) == stamp:
            return True
        if ident not in self.pending and len(self.pending) >= self.limit:
            return False
        self.pending[ident] = Pending(path, stamp, self.clock() + self.quiet)
        return True

    def take(self, plans, maximum=50):
        batch = []
        chosen = None
        now = self.clock()
        # Do not stat files still in their quiet period. At most 64 due entries
        # receive filesystem validation in one tick, including deleted/unsafe files.
        checked = 0
        for ident, item in list(self.pending.items()):
            key, path = ident
            plan = plans.get(key)
            if not plan:
                self.pending.pop(ident, None)
                continue
            if item.due > now or (chosen is not None and chosen != key):
                continue
            checked += 1
            stamp = signature(path)
            if not stamp or not allowed_file(plan, path) or self.completed.get(ident) == stamp:
                self.pending.pop(ident, None)
            elif stamp != item.signature:
                item.signature, item.due = stamp, now + self.quiet
            else:
                chosen = key
                batch.append(item)
                self.pending.pop(ident)
            if len(batch) == maximum or checked >= 64:
                break
        return chosen, batch

    def retain(self, keys):
        self.pending = {ident: value for ident, value in self.pending.items() if ident[0] in keys}
        self.completed = {ident: value for ident, value in self.completed.items() if ident[0] in keys}


@dataclass
class Media:
    token: str
    path: str
    label: str
    device: tuple
    present: bool = True

    def available(self):
        try:
            info = Path(self.path).stat()
            return self.present and (info.st_dev, info.st_ino) == self.device
        except OSError:
            return False


class Automation:
    # Repeated clamscan database loading is expensive. Batch queued arrivals and
    # leave time for foreground work even after a long-running scan finishes.
    START_INTERVAL = 15
    REST_INTERVAL = 2

    def __init__(self, store, dispatch, finished, changed=lambda: None, clock=time.monotonic, scanner=None):
        self.store, self.dispatch, self.finished, self.changed = store, dispatch, finished, changed
        self.clock = clock
        self.scanner = scanner
        self.queue = WatchQueue(clock)
        self.plans = {}
        self.media_enabled = False
        self.media = {}
        self.media_pending = []
        self.media_seen = set()
        self.active = False
        self.next_start = float('-inf')
        self.active_key = None
        self.active_media = None
        self.cancel = threading.Event()
        self.errors = {}
        self.warning_at = {}
        self.closed = False

    def configure(self, plans, media_enabled):
        self.plans = dict(plans)
        self.media_enabled = media_enabled
        self.queue.retain(self.plans)
        if not media_enabled:
            self.media_pending.clear()
        self.errors = {key: value for key, value in self.errors.items() if key in self.plans or key == MEDIA_ID}

    def warn(self, key, plan, message):
        self.errors[key] = message
        if self.clock() - self.warning_at.get(key, float('-inf')) >= 30:
            self.warning_at[key] = self.clock()
            now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
            try:
                self.store.save(now, plan.describe(), '検査不完了：' + message, message, key)
            except Exception as error:
                self.errors[key] += '\n履歴も保存できませんでした：' + str(error)
            self.changed()

    def file_event(self, key, path):
        plan = self.plans.get(key)
        if plan and not self.queue.put(key, plan, path, changed=True):
            self.warn(key, plan, '検査待ちが1000件を超えました。取りこぼし分はフォルダー全体を検査してください。')

    def add_media(self, medium, force=False):
        self.media[medium.token] = medium
        if not self.media_enabled or not medium.available():
            return
        if medium.token == self.active_media or medium.token in self.media_pending:
            return
        if medium.token in self.media_seen and not force:
            return
        if len(self.media_pending) >= 32:
            self.warn(MEDIA_ID, media_plan(), '外部メディアの検査待ちが上限に達しました。接続済みメディアを手動で検査してください。')
            return
        self.media_pending.append(medium.token)
        self.media_seen.add(medium.token)

    def remove_media(self, token):
        medium = self.media.pop(token, None)
        waiting = token in self.media_pending
        if medium:
            medium.present = False
        self.media_pending = [value for value in self.media_pending if value != token]
        self.media_seen.discard(token)
        if waiting and medium:
            from dataclasses import replace
            from .core import record_scan_failure
            plan = replace(media_plan(), label='外部メディア検査：' + medium.label, targets=[medium.path])
            status, output = record_scan_failure(self.store, plan, ValueError('検査待ちのメディアが取り外されたため、検査を開始できませんでした。'), MEDIA_ID)
            self.finished(MEDIA_ID, status, output, plan.label)
        # The active job keeps its Medium object and detects its removal itself.

    def status(self, key):
        if key in self.errors:
            return self.errors[key]
        count = len(self.media_pending) if key == MEDIA_ID else sum(ident[0] == key for ident in self.queue.pending)
        if count:
            waiting = '（負荷を抑えて待機中）' if self.clock() < self.next_start else ''
            return f'検査待ち：{count}件' + waiting
        return '監視中（アプリ起動中のみ）'

    def tick(self, blocked=False):
        if self.closed or self.active or blocked or self.clock() < self.next_start:
            return
        import shutil
        if not shutil.which('clamscan'):
            return  # Retain the bounded queue until the engine becomes available.
        medium = None
        while self.media_pending:
            token = self.media_pending.pop(0)
            candidate = self.media.get(token)
            if self.media_enabled and candidate and candidate.available():
                medium = candidate
                break
        batch = []
        if medium:
            from dataclasses import replace
            key, plan = MEDIA_ID, replace(media_plan(), targets=[medium.path], label='外部メディア検査：' + medium.label)
        else:
            key, batch = self.queue.take(self.plans)
            if not batch:
                return
            plan = self.plans[key]
        self.next_start = self.clock() + self.START_INTERVAL
        self.active, self.active_key = True, key
        self.active_media = medium.token if medium else None
        self.cancel = threading.Event()
        def worker():
            from .core import scan, ScanBusyError, record_scan_failure
            retry = False
            try:
                scanner = self.scanner or scan
                kwargs = {'profile_id': key}
                if medium:
                    kwargs['available'] = medium.available
                else:
                    kwargs['files'] = [item.path for item in batch]
                status, output = execute_scan(plan, self.store, self.cancel, scanner=scanner, **kwargs)
            except (ScanBusyError, ScanAlreadyQueued):
                retry, status, output = True, '', ''
            except Exception as error:
                status, output = record_scan_failure(self.store, plan, error, key)
            def done():
                self.next_start = max(self.next_start, self.clock() + self.REST_INTERVAL)
                self.active, self.active_key, self.active_media = False, None, None
                if retry and not self.closed:
                    if medium and self.media_enabled and medium.available():
                        self.media_pending.insert(0, medium.token)
                    elif key in self.plans:
                        for item in batch:
                            self.queue.put(key, self.plans[key], item.path)
                elif not retry:
                    if assess(status, output).completed:
                        for item in batch:
                            if signature(item.path) == item.signature and (key, item.path) not in self.queue.pending:
                                self.queue.completed[key, item.path] = item.signature
                        # Bound deduplication memory independently of the event queue.
                        while len(self.queue.completed) > 5000:
                            self.queue.completed.pop(next(iter(self.queue.completed)))
                    self.finished(key, status, output, plan.label)
                return False
            self.dispatch(done)
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as error:
            self.next_start = max(self.next_start, self.clock() + self.REST_INTERVAL)
            self.active, self.active_key, self.active_media = False, None, None
            from .core import record_scan_failure
            status, output = record_scan_failure(self.store, plan, error, key)
            self.finished(key, status, output, plan.label)

    def close(self):
        self.closed = True
        self.queue.pending.clear()
        self.media_pending.clear()
        self.cancel.set()
