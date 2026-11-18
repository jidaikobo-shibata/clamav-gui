"""Bounded, durable notification delivery. Critical backlog is summarized, not lost."""
import threading
import time
import uuid


class NotificationOutbox:
    LIMIT = 256

    def __init__(self, store, deliver, clock=time.time):
        self.store, self.deliver, self.clock = store, deliver, clock
        self.stop = threading.Event()
        self.thread = None
        self.guard = threading.Lock()
        self.retry_after = 0
        self.next_poll = 0
        self.state_at = float('-inf')
        self.state_value = None
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS notification_outbox '
                       '(id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, '
                       'urgency TEXT NOT NULL, merge_key TEXT UNIQUE, count INTEGER NOT NULL, '
                       'attempts INTEGER NOT NULL, due REAL NOT NULL, claim TEXT, lease_until REAL NOT NULL DEFAULT 0)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(notification_outbox)')}
            for column, definition in [('claim', 'TEXT'), ('lease_until', 'REAL NOT NULL DEFAULT 0')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE notification_outbox ADD COLUMN {column} {definition}')

    def enqueue(self, title, body, urgency='normal'):
        self.next_poll = 0
        self.state_at = float('-inf')
        urgency = 'critical' if urgency == 'critical' else 'normal'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM notification_outbox WHERE merge_key IS NULL').fetchone()[0]
            if count < self.LIMIT:
                db.execute('INSERT INTO notification_outbox (title,body,urgency,count,attempts,due) VALUES (?,?,?,1,0,?)',
                           (title[:256], body[:8192], urgency, self.clock()))
            else:
                # Two reserved summary rows bound storage while retaining every notice count.
                db.execute('INSERT INTO notification_outbox (title,body,urgency,merge_key,count,attempts,due) '
                           'VALUES (?,?,?,?,1,0,?) ON CONFLICT(merge_key) DO UPDATE SET count=count+1',
                           ('ClamAV 自動検査の通知（まとめ）', '', urgency, urgency, self.clock()))

    def state(self):
        with self.store.connect() as db:
            count = db.execute('SELECT COALESCE(sum(count),0) FROM notification_outbox').fetchone()[0]
        return dict(pending=count, error=self.store.preferences.get('notification-error', ''))

    def cached_state(self):
        now = time.monotonic()
        if now - self.state_at >= 1:
            self.state_value = self.state()
            self.state_at = now
        return self.state_value

    def deliver_one(self):
        claim = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,title,body,urgency,merge_key,count,attempts FROM notification_outbox '
                             "WHERE due<=? AND (claim IS NULL OR lease_until<=?) ORDER BY (urgency='critical') DESC,id LIMIT 1",
                             (self.clock(),self.clock())).fetchone()
            if row is not None:
                db.execute('UPDATE notification_outbox SET claim=?,lease_until=? WHERE id=?',
                           (claim,self.clock()+60,row[0]))
        if row is None:
            return False
        ident, title, body, urgency, merge, count, attempts = row
        if merge:
            detail = '検出・警告または検査不完了' if urgency == 'critical' else '正常終了'
            body = f'{detail}の通知が{count}件あります。検査プランのログを確認してください。'
        try:
            self.deliver(title, body, urgency)
        except Exception as error:
            with self.store.connect() as db:
                db.execute('UPDATE notification_outbox SET attempts=attempts+1,due=?,claim=NULL,lease_until=0 WHERE id=? AND claim=?',
                           (self.clock()+min(300, 5*2**min(attempts, 6)), ident, claim))
                self.store.preferences.set('notification-error', str(error)[:256], db)
            print('通知は未配信のため保留し、再試行します：' + str(error), flush=True)
        else:
            with self.store.connect() as db:
                # New notices may have joined a summary during delivery. Acknowledge only
                # the captured count so these new arrivals remain queued.
                db.execute('DELETE FROM notification_outbox WHERE id=? AND count<=? AND claim=?', (ident, count, claim))
                db.execute('UPDATE notification_outbox SET count=count-?,attempts=0,due=?,claim=NULL,lease_until=0 WHERE id=? AND count>? AND claim=?',
                           (count, self.clock(), ident, count, claim))
                if db.execute('SELECT 1 FROM notification_outbox WHERE attempts>0 LIMIT 1').fetchone() is None:
                    self.store.preferences.set('notification-error', '', db)
        return True

    def pump(self):
        with self.guard:
            if self.stop.is_set() or (self.thread and self.thread.is_alive()) or time.monotonic() < self.retry_after:
                return
            if time.monotonic() < self.next_poll:
                return
            self.next_poll = time.monotonic() + 1
            with self.store.connect() as db:
                ready = db.execute('SELECT 1 FROM notification_outbox WHERE due<=? AND (claim IS NULL OR lease_until<=?) LIMIT 1', (self.clock(),self.clock())).fetchone()
            if not ready:
                return
            def worker():
                try:
                    while not self.stop.is_set() and self.deliver_one():
                        pass
                except Exception as error:
                    self.retry_after = time.monotonic()+5
                    print('通知の配信状態を更新できません。未配信通知を保持します：' + str(error), flush=True)
            self.thread = threading.Thread(target=worker, daemon=False)
            try:
                self.thread.start()
            except Exception:
                self.retry_after = time.monotonic()+5
                self.thread = None
                raise

    def close(self):
        self.stop.set()
        if self.thread:
            # The OS delivery adapter has its own timeout; remaining rows survive restart.
            self.thread.join(timeout=12)
