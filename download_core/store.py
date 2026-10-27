import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from .models import CoreError, canonical

ACTIVE = ('waiting', 'running', 'retry_wait', 'blocked', 'recovery_required')


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.lockfile = open(self.root / 'writer.lock', 'a+b')
        os.chmod(self.root / 'writer.lock', 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                self.lockfile.seek(0)
                if self.lockfile.read(1) == b'':
                    self.lockfile.write(b'0'); self.lockfile.flush()
                self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise CoreError('STORAGE_ERROR', '此数据目录已被另一个核心进程使用')
        self.db = None
        try:
            self.db = sqlite3.connect(self.root / 'state.sqlite3', check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            os.chmod(self.root / 'state.sqlite3', 0o600)
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise CoreError('STORAGE_ERROR', '数据版本不兼容，拒绝写入')
            if version == 0 and self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                raise CoreError('STORAGE_ERROR', '未知数据格式，拒绝初始化')
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 1 and tables != {'documents', 'subscriptions', 'jobs', 'downloads'}:
                raise CoreError('STORAGE_ERROR', '核心数据库结构不完整，拒绝补建覆盖')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_mode=DELETE')
            if version == 0: self.db.executescript('''
                CREATE TABLE IF NOT EXISTS documents(kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, identity TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active ON jobs(identity) WHERE status IN ('waiting','running','retry_wait','blocked','recovery_required');
                CREATE TABLE IF NOT EXISTS downloads(chapter_key TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, state TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')
        except Exception:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def put(self, kind, item_id, body):
        with self.lock:
            self.db.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                            (kind, item_id, canonical(body)))

    def get(self, kind, item_id):
        with self.lock:
            row = self.db.execute('SELECT body FROM documents WHERE kind=? AND id=?', (kind, item_id)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM documents WHERE kind=? ORDER BY rowid', (kind,))]

    def subscriptions(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM subscriptions ORDER BY rowid')]

    def subscription(self, item_id):
        return next((s for s in self.subscriptions() if s['id'] == item_id), None)

    def save_subscription(self, body):
        with self.lock:
            self.db.execute('INSERT INTO subscriptions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                            (body['id'], body['identity'], canonical(body)))

    def jobs(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM jobs ORDER BY rowid')]

    def job(self, item_id):
        with self.lock:
            row = self.db.execute('SELECT body FROM jobs WHERE id=?', (item_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_job(self, body):
        with self.lock:
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,body=excluded.body',
                            (body['id'], body['identity'], body['status'], canonical(body)))

    def completed(self, chapter_key):
        with self.lock:
            row = self.db.execute('SELECT artifact_id FROM downloads WHERE chapter_key=? AND state=?', (chapter_key, 'complete')).fetchone()
            return self.get('artifact', row[0]) if row else None

    def download_record(self, chapter_key):
        with self.lock:
            row = self.db.execute('SELECT artifact_id,state FROM downloads WHERE chapter_key=?', (chapter_key,)).fetchone()
            return dict(row) if row else None

    def commit_artifact(self, artifact):
        with self.transaction():
            artifact['stage'] = 'committed'
            self.put('artifact', artifact['id'], artifact)
            self.db.execute('INSERT INTO downloads VALUES(?,?,?) ON CONFLICT(chapter_key) DO UPDATE SET artifact_id=excluded.artifact_id,state=excluded.state',
                            (artifact['chapter_key'], artifact['id'], 'complete'))
            job = self.job(artifact['job_id'])
            done = set(job.get('done_keys', [])); done.add(artifact['chapter_key'])
            job.update(done_keys=sorted(done), chapters_done=len(done), cbz_path=artifact['relative_path'])
            self.save_job(job)

    def invalidate(self, artifact):
        with self.lock:
            self.db.execute("UPDATE downloads SET state='corrupt' WHERE artifact_id=?", (artifact['id'],))

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if not self.lockfile.closed:
            if os.name == 'nt':
                import msvcrt
                self.lockfile.seek(0)
                try: msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError: pass
            self.lockfile.close()
