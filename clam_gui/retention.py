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


def prune(db, values, now=None):
    validate(values)
    if not values['retention_enabled']:
        return 0
    now = now or datetime.datetime.now().astimezone()
    # Compute old records lazily, only after the user opts into retention.
    db.execute('UPDATE scans SET log_bytes=length(CAST(output AS BLOB))+length(CAST(target AS BLOB))+'
               'length(CAST(status AS BLOB))+128 WHERE log_bytes IS NULL')
    rows = db.execute('SELECT rowid, started, completed_at, status, detected, log_bytes, result_kind FROM scans '
                      'ORDER BY rowid').fetchall()
    expired, normal, important = [], [], []
    total = sum(row[5] for row in rows)
    for ident, started, completed, status, detected, size, kind in rows:
        alert = not assess(status, kind=kind, detected=detected).clean
        days = values['alert_days'] if alert else values['retention_days']
        try:
            stamp = datetime.datetime.fromisoformat(completed or started)
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            old = now - stamp > datetime.timedelta(days=days)
        except (ValueError, TypeError):
            old = False  # Unparseable legacy dates are still subject to hard limits.
        if old:
            expired.append(ident)
            total -= size
        else:
            (important if alert else normal).append((ident, size))
    count = len(rows) - len(expired)
    remove = list(expired)
    for ident, size in normal + important:
        if total <= values['max_mib'] * 1024 * 1024 and count <= values['max_records']:
            break
        remove.append(ident)
        total -= size
        count -= 1
    db.executemany('DELETE FROM scans WHERE rowid=?', [(ident,) for ident in remove])
    return len(remove)
