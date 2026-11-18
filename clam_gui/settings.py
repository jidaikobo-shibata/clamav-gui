"""JSON preferences, with caller-owned transactions for atomic multi-setting saves."""
import json


class Preferences:
    def __init__(self, connect):
        self.connect = connect

    def get_many(self, keys):
        keys = tuple(keys)
        if not keys:
            return {}
        with self.connect() as db:
            rows = db.execute('SELECT key,value FROM preferences WHERE key IN (' +
                              ','.join('?' for _ in keys) + ')', keys).fetchall()
        return {key: json.loads(value) for key, value in rows}

    def get(self, key, default=None):
        return self.get_many([key]).get(key, default)

    def set_many(self, values, db=None):
        if db is None:
            with self.connect() as connection:
                self.set_many(values, connection)
            return
        db.executemany('INSERT INTO preferences VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()])

    def set(self, key, value, db=None):
        self.set_many({key: value}, db)
