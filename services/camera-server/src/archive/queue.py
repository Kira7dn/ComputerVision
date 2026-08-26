"""SQLite-backed durable archive queue."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path


class UploadQueue:
    def __init__(self, database_path, spool_root):
        self.database_path = Path(database_path)
        self.spool_root = Path(spool_root).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(self, path):
        source = Path(path).resolve()
        if self.spool_root != source and self.spool_root not in source.parents:
            raise ValueError("upload source is outside spool root")
        stat = source.stat()
        relative = source.relative_to(self.spool_root).as_posix()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO uploads(relative_path,size,mtime_ns,status,attempts,next_attempt_at)
                   VALUES (?, ?, ?, 'pending', 0, 0)
                   ON CONFLICT(relative_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                   status=CASE WHEN uploads.size=excluded.size AND uploads.mtime_ns=excluded.mtime_ns THEN uploads.status ELSE 'pending' END,
                   attempts=CASE WHEN uploads.size=excluded.size AND uploads.mtime_ns=excluded.mtime_ns THEN uploads.attempts ELSE 0 END,
                   next_attempt_at=CASE WHEN uploads.size=excluded.size AND uploads.mtime_ns=excluded.mtime_ns THEN uploads.next_attempt_at ELSE 0 END""",
                (relative, stat.st_size, stat.st_mtime_ns),
            )
        return relative

    def reconcile(self, extensions):
        return sum(self.enqueue(path) is not None for path in self.spool_root.rglob("*") if path.is_file() and path.suffix.lower() in extensions)

    def claim(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id,relative_path,size,mtime_ns,attempts FROM uploads WHERE status IN ('pending','failed') AND next_attempt_at <= ? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE uploads SET status='uploading', attempts=attempts+1 WHERE id=?", (row["id"],))
            return dict(row)

    def complete(self, item_id, object_key, etag, checksum):
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE uploads SET status='uploaded', object_key=?, etag=?, checksum=?, uploaded_at=?, last_error=NULL WHERE id=?", (object_key, etag, checksum, time.time(), item_id))

    def fail(self, item_id, message, attempts):
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE uploads SET status='failed', last_error=?, next_attempt_at=? WHERE id=?", (str(message)[:1000], time.time() + min(300, 2 ** min(attempts, 8)), item_id))

    def recover_interrupted(self):
        with closing(self._connect()) as connection, connection:
            return connection.execute("UPDATE uploads SET status='pending', next_attempt_at=0 WHERE status='uploading'").rowcount

    def snapshot(self):
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT relative_path,size,status,attempts,object_key,etag,uploaded_at,last_error FROM uploads ORDER BY id")]

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS uploads(id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0, object_key TEXT, etag TEXT, checksum TEXT, uploaded_at REAL, last_error TEXT)")

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection
