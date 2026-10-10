"""Local-only administration of the single integration bearer token."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from api.security import atomic_private, check_parents, fsync_directory, read_regular

TOKEN_FILE = Path('/etc/civicrm-bridge-api/token.json')


def generate(path, output, group=None):
    path, output = Path(path), Path(output)
    if path == output:
        raise ValueError('Token output must differ from digest path')
    check_parents(output)
    token = secrets.token_urlsafe(32)
    # Never overwrite a secret delivery file, including a dangling symlink.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(token.encode('ascii') + b'\n')
            target.flush()
            os.fsync(target.fileno())
        fsync_directory(output.parent)
        record = {'version': 1, 'digest': hashlib.sha256(token.encode('ascii')).hexdigest()}
        atomic_private(path, (json.dumps(record) + '\n').encode(),
                       mode=0o640 if group is not None else 0o600,
                       owner=os.geteuid(), group=group)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def revoke(path, group=None):
    atomic_private(path, b'{"version": 1, "digest": null}\n',
                   mode=0o640 if group is not None else 0o600,
                   owner=os.geteuid(), group=group)


def active(path):
    try:
        value = json.loads(read_regular(path, 4096, {0, os.geteuid()}, forbidden=0o037))
        return value.get('version') == 1 and isinstance(value.get('digest'), str) and len(value['digest']) == 64
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('generate', 'revoke', 'status'))
    parser.add_argument('--output', type=Path, help='New private delivery file, required for generate')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run locally as the administrator with sudo')
    if (args.command == 'generate') != (args.output is not None):
        parser.error('--output is required only for generate')
    import pwd
    account = pwd.getpwnam('civicrm_bridge_api')
    check_parents(TOKEN_FILE)
    fd = os.open(TOKEN_FILE.parent / 'token.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise OSError('Unsafe token lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        if args.command == 'generate':
            generate(TOKEN_FILE, args.output, account.pw_gid)
            print('Token replaced. Secret written to the private output file; previous token is invalid.')
        elif args.command == 'revoke':
            revoke(TOKEN_FILE, account.pw_gid)
            print('Token revoked. API requests are denied until a replacement is generated.')
        else:
            print(json.dumps({'active': active(TOKEN_FILE)}))
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit('Token operation failed; inspect paths and permissions. No secret was printed.')
