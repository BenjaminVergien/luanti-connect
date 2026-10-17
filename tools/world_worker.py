#!/usr/bin/env python3
"""Validate one lifecycle JSON request and invoke the restricted local controller."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifecycle.instance_contract import validate as validate_instance

MAX_BYTES = 16384
COMMAND = ('/usr/bin/sudo', '-n', '/usr/local/sbin/civicrm-worldctl')
OPERATIONS = ('status', 'stop_world', 'start_world', 'switch_world')
WORLDS = ('dev', 'test')


def failure(code, message, job_id=None):
    return {'version': 1, 'job_id': job_id, 'ok': False,
            'error': {'code': code, 'message': message}}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def validate(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Request exceeds 16 KiB.')
    request = decode(raw)
    if not isinstance(request, dict):
        raise ValueError('Request must be an object.')
    if type(request.get('version')) is int and request['version'] == 2:
        validate_instance(request)
        return request, [*COMMAND, 'instance']
    if type(request.get('version')) is not int or request['version'] != 1:
        raise ValueError('version must be integer 1.')
    job_id = request.get('job_id')
    if not isinstance(job_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id) is None:
        raise ValueError('Invalid job_id.')
    operation = request.get('operation')
    if operation not in OPERATIONS:
        raise ValueError('Unknown operation.')
    fields = {'version', 'job_id', 'operation'}
    arguments = [*COMMAND, operation]
    if operation in ('start_world', 'switch_world'):
        fields.add('world_id')
        if request.get('world_id') not in WORLDS:
            raise ValueError('world_id must be dev or test.')
        arguments.append(request['world_id'])
    if set(request) != fields:
        raise ValueError('Missing or unexpected request fields.')
    return request, arguments


def invoke(arguments, raw=None):
    try:
        process = subprocess.run(arguments, **({'input': raw} if raw is not None else {'stdin': subprocess.DEVNULL}),
                                 capture_output=True, timeout=600, check=False,
                                 env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'})
    except subprocess.TimeoutExpired:
        return failure('outcome_unknown', 'Controller timed out; inspect fresh status before another decision.')
    except OSError:
        return failure('launch_failed', 'Cannot launch the restricted controller.')
    try:
        result = decode(process.stdout)
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise ValueError('Invalid result')
        if result['ok']:
            if process.returncode != 0:
                raise ValueError('Exit code mismatch')
        else:
            error = result.get('error')
            if process.returncode != 1 or not isinstance(error, dict):
                raise ValueError('Invalid error')
            if not isinstance(error.get('code'), str) or not isinstance(error.get('message'), str):
                raise ValueError('Invalid error fields')
            if 'details' in error and not isinstance(error['details'], dict):
                raise ValueError('Invalid details')
        return result
    except (ValueError, UnicodeError):
        # Do not expose stderr or assume a non-JSON error means no side effects.
        result = failure('controller_process_error',
                         'Controller returned no valid JSON outcome; inspect fresh status.')
        result['error']['details'] = {'exit_code': process.returncode}
        return result


def private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise OSError('Unsafe worker state file')
    return fd


def handle(raw, state_dir, runner=invoke):
    try:
        request, arguments = validate(raw)
    except (ValueError, UnicodeError):
        return failure('invalid_request', 'Invalid lifecycle request; see the worker contract.')
    job_id = request['job_id']
    digest = hashlib.sha256(raw).hexdigest()
    lock = None
    db = None
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise OSError('Unsafe state directory')
        lock = private_file(state_dir / 'worker.lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return failure('busy', 'Another worker request is running; retry the same bytes later.', job_id)
        fd = private_file(state_dir / 'receipts.sqlite3')
        os.close(fd)
        db = sqlite3.connect(state_dir / 'receipts.sqlite3')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)')
        previous = db.execute('SELECT digest, result FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if previous:
            if previous[0] != digest:
                return failure('job_id_conflict', 'This job_id was used with different request bytes.', job_id)
            if previous[1] is None and request['version'] == 1:
                return failure('outcome_unknown', 'Interrupted request; inspect fresh status. This ID will not execute again.', job_id)
            if previous[1] is not None:
                return json.loads(previous[1])
        # Commit intent before invoking: a crash cannot cause an automatic replay.
        db.execute('INSERT OR IGNORE INTO jobs VALUES (?, ?, NULL)', (job_id, digest))
        db.commit()
        outcome = runner(arguments, raw) if request['version'] == 2 else runner(arguments)
        result = {**outcome, 'version': request['version'], 'job_id': job_id}
        if request['version'] == 2:
            result['instance_id'] = request['instance_id']
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (json.dumps(result), job_id))
        db.commit()
        return result
    except (OSError, sqlite3.Error, ValueError):
        return failure('worker_state_error', 'Worker state unavailable; inspect status before retrying.', job_id)
    finally:
        if db is not None:
            db.close()
        if lock is not None:
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', nargs='?', default='-', help='JSON file, or - for stdin')
    parser.add_argument('--state-dir', type=Path,
                        default=Path.home() / '.local/state/civicrm-world-worker')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.request == '-':
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(args.request, 'rb') as source:
                raw = source.read(MAX_BYTES + 1)
        result = handle(raw, args.state_dir)
    except OSError:
        result = failure('input_error', 'Cannot read request file.')
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
