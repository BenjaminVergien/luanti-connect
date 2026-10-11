"""Durable acceptance, exact-byte replay, and conservative crash recovery."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

from api.security import check_parents, fsync_directory
from tools import world_worker


class Conflict(Exception):
    pass


@contextmanager
def runtime_directory(path):
    """Traverse held no-follow descriptors; runtime ancestors allow traversal only."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OSError('Invalid runtime path')
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_job(directory, name, owner):
    with runtime_directory(directory) as parent:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (owner, os.geteuid())
                    or info.st_mode & 0o007 or info.st_nlink != 1):
                raise OSError('Unsafe job file')
            raw = source.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError('Oversized job file')
            return raw


def publish(directory, name, raw):
    with runtime_directory(directory) as parent:
        temporary = '.' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o660, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            # O_PATH cannot fsync; the inbox grants directory read access.
            sync_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)
        finally:
            os.unlink(temporary, dir_fd=parent)


def unknown(job_id):
    return world_worker.failure('outcome_unknown',
                                'Dispatch was interrupted; inspect fresh status and resolve before another decision.', job_id)


class Jobs:
    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or world_worker.invoke
        self.state = Path(config['state_dir'])
        check_parents(self.state)
        self.state.mkdir(mode=0o700, exist_ok=True)
        info = self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError('Unsafe API state')
        self.lock = world_worker.private_file(self.state / 'service.lock')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = world_worker.private_file(self.state / 'jobs.sqlite3')
            os.fsync(fd)
            os.close(fd)
            fsync_directory(self.state)
            self.db = sqlite3.connect(self.state / 'jobs.sqlite3', check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                kind TEXT NOT NULL, world TEXT NOT NULL, job_id TEXT NOT NULL,
                digest TEXT NOT NULL, raw BLOB, phase TEXT NOT NULL,
                baseline TEXT, result TEXT, PRIMARY KEY(kind, world, job_id))''')
            self.db.commit()
            self.mutex = threading.RLock()
            self.recover()
        except Exception:
            if hasattr(self, 'db'):
                self.db.close()
            os.close(self.lock)
            raise

    def close(self):
        self.db.close()
        os.close(self.lock)

    def rows(self, sql, arguments=()):
        with self.mutex:
            return self.db.execute(sql, arguments).fetchall()

    def update(self, row, phase, result=None, baseline=None):
        with self.mutex, self.db:
            self.db.execute('''UPDATE jobs SET phase=?, result=?, baseline=?,
                raw=CASE WHEN ? IN ('completed','outcome_unknown') THEN NULL ELSE raw END
                WHERE kind=? AND world=? AND job_id=?''',
                (phase, json.dumps(result) if result is not None else None, baseline, phase,
                 row['kind'], row['world'], row['job_id']))

    def get(self, kind, world, job_id):
        rows = self.rows('SELECT * FROM jobs WHERE kind=? AND world=? AND job_id=?', (kind, world, job_id))
        if not rows:
            return None
        row = rows[0]
        state = row['phase'] if row['phase'] in ('completed', 'outcome_unknown') else 'pending'
        prefix = f'/v1/worlds/{world}/accounts' if kind == 'accounts' else '/v1/lifecycle'
        value = {'version': 1, 'job_id': job_id, 'state': state,
                 'result_url': f'{prefix}/jobs/{job_id}'}
        if kind == 'accounts':
            value['world_id'] = world
        if row['result'] is not None:
            value['result'] = json.loads(row['result'])
        return value

    def accept(self, kind, world, job_id, raw):
        digest = hashlib.sha256(raw).hexdigest()
        with self.mutex, self.db:
            previous = self.db.execute('SELECT digest FROM jobs WHERE kind=? AND world=? AND job_id=?',
                                       (kind, world, job_id)).fetchone()
            if previous and previous['digest'] != digest:
                raise Conflict()
            if previous is None:
                self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL,NULL)',
                                (kind, world, job_id, digest, raw, 'queued'))
        return self.get(kind, world, job_id)

    def result_bytes(self, row):
        directory = Path(self.config['worlds'][row['world']]) / 'outbox'
        return read_job(directory, row['job_id'] + '.json', self.config['world_owner_uid'])

    def pending_bytes(self, row):
        directory = Path(self.config['worlds'][row['world']]) / 'inbox'
        return read_job(directory, row['job_id'] + '.json', self.config['world_owner_uid'])

    def account_result(self, row, raw):
        result = world_worker.decode(raw)
        if (not isinstance(result, dict) or type(result.get('version')) is not int
                or result['version'] not in (1, 2)
                or result.get('job_id') != row['job_id']
                or result.get('status') not in ('ok', 'partial', 'error')
                or not isinstance(result.get('accounts'), list)):
            raise ValueError('Invalid bridge result')
        # Project only contracted result fields; never echo an unexpected secret.
        safe = {key: result[key] for key in ('version', 'job_id', 'status', 'code') if key in result}
        safe['accounts'] = [{key: entry[key] for key in ('username', 'status', 'code') if key in entry}
                            for entry in result['accounts'] if isinstance(entry, dict)]
        self.update(row, 'completed', safe)

    def recover(self):
        for row in self.rows("SELECT * FROM jobs WHERE phase='dispatching'"):
            if row['kind'] == 'lifecycle':
                # Existing worker receipts replay; a missing receipt records unknown
                # through a runner that cannot invoke the controller.
                result = world_worker.handle(row['raw'], self.state / 'lifecycle',
                                             runner=lambda args: unknown(row['job_id']))
                self.finish_lifecycle(row, result)
            else:
                try:
                    self.recover_account(row)
                except (OSError, ValueError, UnicodeError):
                    self.update(row, 'outcome_unknown', unknown(row['job_id']))

    def recover_account(self, row):
        try:
            pending = self.pending_bytes(row)
        except FileNotFoundError:
            try:
                result = self.result_bytes(row)
            except FileNotFoundError:
                result = None
            if result is not None and hashlib.sha256(result).hexdigest() != row['baseline']:
                self.account_result(row, result)
            else:
                self.update(row, 'outcome_unknown', unknown(row['job_id']))
        else:
            if hashlib.sha256(pending).hexdigest() == row['digest']:
                self.update(row, 'published')
            else:
                self.update(row, 'outcome_unknown', unknown(row['job_id']))

    def tick_accounts(self):
        for row in self.rows("SELECT * FROM jobs WHERE kind='accounts' AND phase='queued' ORDER BY rowid"):
            try:
                try:
                    baseline = hashlib.sha256(self.result_bytes(row)).hexdigest()
                except FileNotFoundError:
                    baseline = None
                self.update(row, 'dispatching', baseline=baseline)
                directory = Path(self.config['worlds'][row['world']]) / 'inbox'
                try:
                    publish(directory, row['job_id'] + '.json', row['raw'])
                except FileExistsError:
                    if hashlib.sha256(self.pending_bytes(row)).hexdigest() != row['digest']:
                        self.update(row, 'outcome_unknown', unknown(row['job_id']))
                        continue
                self.update(row, 'published')
            except (OSError, ValueError):
                # Dispatch may have succeeded before a filesystem error. Never
                # republish automatically or leave an ambiguous job pending forever.
                current = self.rows('SELECT phase FROM jobs WHERE kind=? AND world=? AND job_id=?',
                                    (row['kind'], row['world'], row['job_id']))[0]
                if current['phase'] == 'dispatching':
                    self.update(row, 'outcome_unknown', unknown(row['job_id']))
                continue
        for row in self.rows("SELECT * FROM jobs WHERE kind='accounts' AND phase='published'"):
            try:
                self.pending_bytes(row)
            except FileNotFoundError:
                try:
                    self.account_result(row, self.result_bytes(row))
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, UnicodeError):
                    self.update(row, 'outcome_unknown', unknown(row['job_id']))
            except (OSError, ValueError):
                continue

    def finish_lifecycle(self, row, result):
        uncertain = result.get('error', {}).get('code') in (
            'outcome_unknown', 'worker_state_error', 'controller_process_error')
        self.update(row, 'outcome_unknown' if uncertain else 'completed', result)

    def tick_lifecycle(self):
        rows = self.rows("SELECT * FROM jobs WHERE kind='lifecycle' AND phase='queued' ORDER BY rowid LIMIT 1")
        if rows:
            row = rows[0]
            self.update(row, 'dispatching')
            result = world_worker.handle(row['raw'], self.state / 'lifecycle', runner=self.runner)
            self.finish_lifecycle(row, result)
