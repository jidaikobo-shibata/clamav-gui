"""Headless GLib monitoring process; no Gtk application or desktop window."""
from .notifications import AutomaticNotices
import json
import threading
import subprocess
import time
from .platforms import get_backend
from .automation import Automation, MEDIA_ID, signature
from .background import Lease, enabled, watched_plans, preference, put_preference
from gi.repository import GLib


def notify(title, body, urgency='normal'):
    try:
        get_backend().notify(title, body, urgency)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print('通知を表示できませんでした。検査履歴を確認してください。', flush=True)


class Checkpoints:
    """Bounded persistent metadata: resume downtime changes after the first baseline."""
    LIMIT = 100000

    def __init__(self, store, engine):
        self.store, self.engine = store, engine
        self.roots, self.ready = {}, set()
        self.files, self.dirty = {}, {}
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS monitor_roots (id TEXT PRIMARY KEY, config TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS monitor_files (id TEXT NOT NULL, path TEXT NOT NULL, stamp TEXT NOT NULL, PRIMARY KEY(id,path))')

    def configure(self, plans):
        configs = {key: json.dumps(dict(targets=plan.targets, excludes=plan.excludes, excluded_dirs=plan.excluded_dirs, recursive=plan.watch_recursive), sort_keys=True) for key, plan in plans}
        if configs == self.roots:
            return
        self.flush()
        self.roots = configs
        self.ready.clear()
        with self.store.connect() as db:
            for key, config in configs.items():
                old = db.execute('SELECT config FROM monitor_roots WHERE id=?', (key,)).fetchone()
                if old and old[0] == config:
                    self.ready.add(key)
                else:
                    db.execute('DELETE FROM monitor_files WHERE id=?', (key,))
                    db.execute('DELETE FROM monitor_roots WHERE id=?', (key,))
            for (key,) in db.execute('SELECT id FROM monitor_roots UNION SELECT id FROM monitor_files').fetchall():
                if key not in configs:
                    db.execute('DELETE FROM monitor_files WHERE id=?', (key,))
                    db.execute('DELETE FROM monitor_roots WHERE id=?', (key,))
            self.files = {(key, path): stamp for key, path, stamp in db.execute('SELECT id,path,stamp FROM monitor_files LIMIT ?', (self.LIMIT,))}

    def inventory(self, key, path):
        stamp = signature(path)
        if stamp is None:
            return
        value = json.dumps(stamp)
        ident = key, path
        old = self.files.get(ident)
        if key in self.ready and old != value:
            self.engine.file_event(key, path)
            return
        if old is None:
            if len(self.files) >= self.LIMIT:
                self.engine.warn(key, self.engine.plans[key], '再開確認用のファイル数上限に達しました。定期的な全体検査を併用してください。')
                return
            self.files[ident] = value
            self.dirty[ident] = value
            if len(self.dirty) >= 512:
                self.flush()

    def flush(self):
        if self.dirty:
            with self.store.connect() as db:
                db.executemany('INSERT INTO monitor_files VALUES (?,?,?) ON CONFLICT(id,path) DO UPDATE SET stamp=excluded.stamp',
                               [(key, path, stamp) for (key, path), stamp in self.dirty.items()])
            self.dirty.clear()

    def complete_baseline(self, monitors):
        self.flush()
        with self.store.connect() as db:
            for key, config in self.roots.items():
                if key not in self.ready and key not in self.engine.errors and (key, str(self.engine.plans[key].targets[0])) in monitors:
                    db.execute('INSERT INTO monitor_roots VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config', (key, config))
                    self.ready.add(key)

    def scanned(self):
        for ident, stamp in self.engine.queue.completed.items():
            value = json.dumps(stamp)
            if self.files.get(ident) == value:
                continue
            if ident not in self.files and len(self.files) >= self.LIMIT:
                key = ident[0]
                if key in self.engine.plans:
                    self.engine.warn(key, self.engine.plans[key], '再開確認用のファイル数上限に達しました。定期的な全体検査を併用してください。')
                continue
            self.files[ident] = value
            self.dirty[ident] = value
        self.flush()


class Monitor:
    def __init__(self, store, observers=None, notifier=notify):
        self.store = store
        self.notification_slots = threading.BoundedSemaphore(4)
        def asynchronous_notify(*args):
            if not self.notification_slots.acquire(blocking=False):
                print('通知が混み合っています。検査履歴を確認してください。', flush=True)
                return
            def send():
                try:
                    notifier(*args)
                finally:
                    self.notification_slots.release()
            threading.Thread(target=send, daemon=False).start()
        self.notifier = asynchronous_notify
        self.loop = GLib.MainLoop()
        self.lease = Lease(store)
        self.revision = time.time()
        self.engine = Automation(store, GLib.idle_add, self.finished, self.changed)
        self.checkpoints = Checkpoints(store, self.engine)
        self.observers = (observers or get_backend().observers)(self.engine)
        self.observers.inventory = self.checkpoints.inventory
        self.notices, self.command = AutomaticNotices(), None
        self.stopping = False
        self.failure = None
        self.last_tick = time.time()
        self.started = False
        self.last_config_time = 0
        self.generation = __import__('uuid').uuid4().hex

    def changed(self):
        self.revision = time.time()

    def finished(self, key, status, output, label=None):
        self.changed()
        notice = self.notices.record(key, label or key, status, output)
        if notice:
            self.notifier(notice.title, notice.body, notice.urgency)
        self.checkpoints.scanned()

    def flush(self, force=False):
        for key in self.notices.due(force):
            notice = self.notices.flush(key)
            self.notifier(notice.title, notice.body, notice.urgency)

    def tick(self):
        if self.stopping or not enabled(self.store):
            self.stop()
            if not self.engine.active:
                self.loop.quit()
                return False
            return True
        if not self.lease.acquire():
            return True  # GUI finishes any scan before yielding monitoring ownership.
        now = time.monotonic()
        resumed = time.time()-self.last_tick > 15
        self.last_tick = time.time()
        if now-self.last_config_time > 2 or resumed:
            plans = watched_plans(self.store)
            media = self.store.automation_settings()['media_enabled']
            self.checkpoints.configure(plans)
            self.engine.configure(plans, media)
            if resumed:
                self.observers.plans = {}  # Re-enumerate metadata after suspension/event gaps.
            self.observers.configure(plans)
            if (resumed or not self.started) and media:
                self.observers.scan_connected()
            self.started = True
            self.last_config_time = now
        if not self.observers.tasks:
            self.checkpoints.complete_baseline(self.observers.monitors)
        command = preference(self.store, 'background-command', {})
        if command and command.get('token') != self.command:
            self.command = command.get('token')
            if command.get('action') == 'media' and time.time()-command.get('created', 0) < 30:
                count = self.observers.scan_connected()
                put_preference(self.store, 'background-command-result', dict(token=self.command, count=count))
        self.engine.tick()
        self.flush()
        statuses = {key: self.engine.status(key) for key in self.engine.plans}
        statuses[MEDIA_ID] = self.engine.status(MEDIA_ID)
        if not __import__('shutil').which('clamscan'):
            statuses = {key: 'ClamAV未導入：検査待ちを保持しています' for key in statuses}
        put_preference(self.store, 'background-state', dict(updated=time.time(), generation=self.generation, revision=self.revision, statuses=statuses, active=self.engine.active_key))
        return True

    def checked_tick(self):
        try:
            return self.tick()
        except Exception as error:
            self.failure = error
            print('常駐監視を停止します：' + str(error), flush=True)
            try:
                self.stop()
            finally:
                GLib.timeout_add(100, self.wait_for_stop)
            return False

    def wait_for_stop(self):
        if self.engine.active:
            return True
        self.loop.quit()
        return False

    def stop(self, *_):
        if not self.stopping:
            self.stopping = True
            try:
                self.observers.close()
            finally:
                self.engine.close()

    def run(self):
        get_backend().install_stop_handlers(self.stop)
        GLib.timeout_add(500, self.checked_tick)
        try:
            self.loop.run()
        finally:
            self.stop()
            self.flush(True)
            self.lease.close()
            if preference(self.store, 'background-state', {}).get('generation') == self.generation:
                put_preference(self.store, 'background-state', {})
        if self.failure:
            raise RuntimeError('常駐監視でエラーが発生しました。') from self.failure
