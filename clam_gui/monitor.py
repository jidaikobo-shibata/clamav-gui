"""Headless GLib monitoring process; no Gtk application or desktop window."""
from .notifications import AutomaticNotices
from .notification_outbox import NotificationOutbox
from .notification_retry import NotificationRetry, report
from .runtime import retry_cleanup
import json
import sqlite3
import time
from .platforms import get_backend
from .automation import Automation, MEDIA_ID, signature, SIGNATURE_VERSION
from .background import Lease, watched_plans, preference, put_preference
from gi.repository import GLib


def notify(title, body, urgency='normal'):
    get_backend().notify(title, body, urgency)


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
        configs = {key: json.dumps(dict(signature_version=SIGNATURE_VERSION, targets=plan.targets, excludes=plan.excludes, excluded_dirs=plan.excluded_dirs, recursive=plan.watch_recursive), sort_keys=True) for key, plan in plans}
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
                db.execute('PRAGMA busy_timeout=0')
                db.executemany('INSERT INTO monitor_files VALUES (?,?,?) ON CONFLICT(id,path) DO UPDATE SET stamp=excluded.stamp',
                               [(key, path, stamp) for (key, path), stamp in self.dirty.items()])
            self.dirty.clear()

    def complete_baseline(self, monitors):
        self.flush()
        if self.ready.issuperset(self.roots):
            return
        completed = set()
        with self.store.connect() as db:
            db.execute('PRAGMA busy_timeout=0')
            for key, config in self.roots.items():
                if key not in self.ready and key not in self.engine.errors and (key, str(self.engine.plans[key].targets[0])) in monitors:
                    db.execute('INSERT INTO monitor_roots VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config', (key, config))
                    completed.add(key)
        self.ready.update(completed)

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
        self.outbox = NotificationOutbox(store, notifier)
        self.notifier = self.outbox.enqueue
        self.notification_retry = NotificationRetry(store.path.parent)
        self.loop = GLib.MainLoop()
        self.lease = Lease(store)
        self.revision = time.time()
        self.engine = Automation(store, GLib.idle_add, self.finished, self.changed)
        self.checkpoints = Checkpoints(store, self.engine)
        self.observers = (observers or get_backend().observers)(self.engine)
        self.observers.inventory = self.checkpoints.inventory
        self.notices, self.command = AutomaticNotices(), None
        self.timer = None
        self.stopping = False
        self.failure = None
        self.last_tick = time.time()
        self.started = False
        self.last_config_time = 0
        self.last_control = self.last_publish = float('-inf')
        self.last_state = None
        self.control = True
        self.command_value = {}
        self.checkpoint_due = self.checkpoint_attempts = 0
        self.checkpoint_error = ''
        self.checkpoint_scans_pending = False
        self.publish_due = 0
        self.generation = __import__('uuid').uuid4().hex

    def changed(self):
        self.revision = time.time()

    def finished(self, key, status, output, label=None):
        self.changed()
        notice = self.notices.record(key, label or key, status, output)
        if notice:
            self.notification_retry.add(notice)
            self.notification_retry.drain(self.notifier)
        self.save_checkpoints()

    def save_checkpoints(self, *, baseline=False):
        if not baseline:
            self.checkpoint_scans_pending = True
        now = time.monotonic()
        if now < self.checkpoint_due:
            return
        try:
            if self.checkpoint_scans_pending:
                self.checkpoints.scanned()
                self.checkpoint_scans_pending = False
            if baseline:
                self.checkpoints.complete_baseline(self.observers.monitors)
        except sqlite3.Error as error:
            self.checkpoint_attempts += 1
            self.checkpoint_due = now + min(300, 5 * 2**min(self.checkpoint_attempts-1, 6))
            self.checkpoint_error = '再開確認情報を保存できません。自動で再試行します：' + str(error)[:256]
            report(self.checkpoint_error)
        else:
            self.checkpoint_due = self.checkpoint_attempts = 0
            self.checkpoint_error = ''

    def flush(self, force=False):
        for key in self.notices.due(force):
            notice = self.notices.flush(key)
            self.notification_retry.add(notice)
        self.notification_retry.drain(self.notifier, force=force)

    def tick(self):
        now = time.monotonic()
        if now - self.last_control >= 1:
            controls = self.store.preferences.get_many(('background-enabled', 'background-command'))
            self.control = controls.get('background-enabled') is True
            self.command_value = controls.get('background-command', {})
            self.last_control = now
        if self.stopping or not self.control:
            self.stop()
            if not self.engine.active:
                self.loop.quit()
                return False
            return True
        if not self.lease.acquire():
            self.last_control = float('-inf')
            return True  # GUI finishes any scan before yielding monitoring ownership.
        try:
            retry_cleanup(self.store, wait=False)
        except sqlite3.Error:
            pass  # The owner retries again; cleanup must not block the GLib thread.
        self.notification_retry.load()
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
        if not getattr(self.observers, 'discovering', self.observers.tasks):
            self.save_checkpoints(baseline=True)
        command = self.command_value
        if command and command.get('token') != self.command:
            self.command = command.get('token')
            if command.get('action') == 'media' and time.time()-command.get('created', 0) < 30:
                count = self.observers.scan_connected()
                put_preference(self.store, 'background-command-result', dict(token=self.command, count=count))
        self.engine.tick()
        self.flush()
        self.outbox.pump()
        statuses = {key: self.engine.status(key) for key in self.engine.plans}
        statuses[MEDIA_ID] = self.engine.status(MEDIA_ID)
        if not __import__('shutil').which('clamscan'):
            statuses = {key: 'ClamAV未導入：検査待ちを保持しています' for key in statuses}
        if self.checkpoint_error:
            statuses = {key: value + '\n' + self.checkpoint_error for key, value in statuses.items()}
        # Publish changed state promptly; keep the eight-second lease fresh at
        # three-second intervals without rewriting identical state twice a second.
        delivery = dict(self.outbox.cached_state())
        delivery['pending'] += self.notification_retry.count
        delivery['error'] = self.notification_retry.error or delivery.get('error', '')
        state = dict(generation=self.generation, revision=self.revision, statuses=statuses,
                     active=self.engine.active_key, notifications=delivery)
        if now >= self.publish_due and (state != self.last_state or now - self.last_publish >= 3):
            try:
                # A locked database also prevents heartbeat writes; keep watching
                # and leave the previous published state untouched until retry.
                with self.store.connect() as db:
                    db.execute('PRAGMA busy_timeout=0')
                    self.store.preferences.set('background-state', dict(updated=time.time(), **state), db=db)
            except sqlite3.Error as error:
                self.publish_due = now + 5
                report('常駐状態の保存を再試行します：' + str(error))
            else:
                self.publish_due = 0
                self.last_state, self.last_publish = state, now
        return True

    def checked_tick(self):
        try:
            keep = self.tick()
            if not keep:
                self.timer = None
            return keep
        except Exception as error:
            self.timer = None
            self.failure = error
            report('常駐監視を停止します：' + str(error))
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

    def shutdown(self):
        """Every resource gets an independent cleanup attempt, despite earlier errors."""
        errors = []
        def attempt(label, action):
            try:
                action()
            except Exception as error:
                errors.append(error)
                report(label + '：' + str(error))
        attempt('監視停止に失敗しました', self.stop)
        attempt('終了時の通知保存に失敗しました', lambda: self.flush(True))
        if self.notification_retry.rows:
            attempt('未保存通知の退避に失敗しました', self.notification_retry.persist)
        attempt('通知配送の停止に失敗しました', self.outbox.close)
        attempt('実行記録の整理に失敗しました', lambda: retry_cleanup(self.store, wait=False))
        def clear_state():
            if preference(self.store, 'background-state', {}).get('generation') == self.generation:
                put_preference(self.store, 'background-state', {})
        # Clear shared state while still owning the lease; never race a new owner.
        attempt('終了状態の保存に失敗しました', clear_state)
        attempt('監視ロックの解放に失敗しました', self.lease.close)
        if errors and self.failure is None:
            self.failure = errors[0]

    def run(self):
        try:
            get_backend().install_stop_handlers(self.stop)
            self.timer = GLib.timeout_add(500, self.checked_tick)
            self.loop.run()
        finally:
            try:
                if self.timer is not None:
                    GLib.source_remove(self.timer)
                    self.timer = None
            finally:
                self.shutdown()
        if self.failure:
            raise RuntimeError('常駐監視でエラーが発生しました。') from self.failure
