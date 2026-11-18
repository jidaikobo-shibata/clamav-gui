"""Explicitly enabled, transactional retention of scan history (logical bytes)."""
import datetime
from .results import assess

DEFAULTS = dict(media_enabled=False, retention_enabled=False, retention_days=90,
                alert_days=365, max_mib=256, max_records=10000)


def validate(values):
    if set(values) != set(DEFAULTS):
        raise ValueError('監視・履歴設定の項目が不正です。')
    for key in ('media_enabled', 'retention_enabled'):
        if type(values[key]) is not bool:
            raise ValueError('監視・履歴設定のオン・オフが不正です。')
    for key, minimum, maximum in [('retention_days', 1, 3650), ('alert_days', 1, 3650),
                                  ('max_mib', 16, 4096), ('max_records', 100, 100000)]:
        if type(values[key]) is not int or not minimum <= values[key] <= maximum:
            raise ValueError('履歴の保存期間・容量・件数を確認してください。')
    if values['alert_days'] < values['retention_days']:
        raise ValueError('検出・不完了の保存期間は、通常の結果以上にしてください。')


def initialize(db):
    """Persist compact retention metadata; triggers invalidate only changed rows.

    Old writers can continue inserting the original columns. Their rows join the
    pending queue and are classified on the next enabled cleanup.
    """
    db.execute('BEGIN IMMEDIATE')
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_pending'").fetchone()
    db.execute('CREATE TABLE IF NOT EXISTS retention_info '
               '(id INTEGER PRIMARY KEY, stamp REAL, alert INTEGER NOT NULL, size INTEGER NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS retention_age ON retention_info(alert,stamp)')
    db.execute('CREATE INDEX IF NOT EXISTS retention_priority ON retention_info(alert,id)')
    db.execute('CREATE TABLE IF NOT EXISTS retention_pending (id INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS retention_totals (id INTEGER PRIMARY KEY, records INTEGER, bytes INTEGER)')
    db.execute('INSERT OR IGNORE INTO retention_totals VALUES (1,0,0)')
    db.execute('CREATE TRIGGER IF NOT EXISTS retention_insert AFTER INSERT ON retention_info BEGIN '
               'UPDATE retention_totals SET records=records+1,bytes=bytes+NEW.size WHERE id=1; '
               'DELETE FROM retention_pending WHERE id=NEW.id; END')
    db.execute('CREATE TRIGGER IF NOT EXISTS retention_delete AFTER DELETE ON retention_info BEGIN '
               'UPDATE retention_totals SET records=records-1,bytes=bytes-OLD.size WHERE id=1; END')
    db.execute('CREATE TRIGGER IF NOT EXISTS scans_retention_insert AFTER INSERT ON scans BEGIN '
               'INSERT OR IGNORE INTO retention_pending VALUES (NEW.rowid); END')
    db.execute('CREATE TRIGGER IF NOT EXISTS scans_retention_update AFTER UPDATE ON scans BEGIN '
               'DELETE FROM retention_info WHERE id=OLD.rowid; '
               'DELETE FROM retention_pending WHERE id=OLD.rowid; '
               'INSERT OR IGNORE INTO retention_pending VALUES (NEW.rowid); END')
    db.execute('CREATE TRIGGER IF NOT EXISTS scans_retention_delete AFTER DELETE ON scans BEGIN '
               'DELETE FROM retention_info WHERE id=OLD.rowid; '
               'DELETE FROM retention_pending WHERE id=OLD.rowid; END')
    if not exists:
        db.execute('INSERT OR IGNORE INTO retention_pending SELECT rowid FROM scans')


def prune(db, values, now=None):
    validate(values)
    if not values['retention_enabled']:
        return 0
    now = now or datetime.datetime.now().astimezone()
    # Only new/modified records require Python parsing. Never fetch log bodies.
    while True:
        rows = db.execute('SELECT s.rowid,s.started,s.completed_at,s.status,s.detected,s.result_kind,'
                          'length(CAST(s.output AS BLOB))+length(CAST(s.target AS BLOB))+'
                          'length(CAST(s.status AS BLOB))+128 FROM retention_pending p '
                          'JOIN scans s ON s.rowid=p.id LIMIT 512').fetchall()
        if not rows:
            break
        for ident, started, completed, status, detected, kind, size in rows:
            try:
                stamp = datetime.datetime.fromisoformat(completed or started).timestamp()
            except (ValueError, TypeError, OverflowError, OSError):
                stamp = None
            alert = not assess(status, kind=kind, detected=detected).clean
            db.execute('INSERT INTO retention_info VALUES (?,?,?,?)', (ident, stamp, int(alert), size or 128))
    removed = 0
    for alert, days in ((0, values['retention_days']), (1, values['alert_days'])):
        cutoff = (now - datetime.timedelta(days=days)).timestamp()
        removed += db.execute('DELETE FROM scans WHERE rowid IN '
                              '(SELECT id FROM retention_info WHERE alert=? AND stamp<?)',
                              (alert, cutoff)).rowcount
    count, total = db.execute('SELECT records,bytes FROM retention_totals WHERE id=1').fetchone()
    remove = []
    if count > values['max_records'] or total > values['max_mib'] * 1024 * 1024:
        for ident, size in db.execute('SELECT id,size FROM retention_info ORDER BY alert,id'):
            if count <= values['max_records'] and total <= values['max_mib'] * 1024 * 1024:
                break
            remove.append((ident,))
            count -= 1
            total -= size
        db.executemany('DELETE FROM scans WHERE rowid=?', remove)
    return removed + len(remove)
