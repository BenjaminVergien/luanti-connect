"""Private filesystem helpers and fail-closed bearer authentication."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def check_parents(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OSError('Expected an absolute path')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError('Unsafe parent directory')
        # /tmp itself is acceptable for disposable fixtures, never its children.
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
            raise OSError('Untrusted parent directory')


def read_regular(path, limit, owners=None, forbidden=0o022):
    check_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (owners is not None and info.st_uid not in owners)
                or info.st_mode & forbidden):
            raise OSError('Unsafe regular file')
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('File too large')
        return raw


def atomic_private(path, raw, mode=0o600, owner=None, group=None):
    """Replace an operator-managed file only inside trusted parent directories."""
    path = Path(path)
    check_parents(path)
    if path.exists() or path.is_symlink():
        read_regular(path, 1024 * 1024, {0, os.geteuid()}, forbidden=0o022)
    fd, name = tempfile.mkstemp(prefix='.bridge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            if owner is not None:
                os.fchown(output.fileno(), owner, -1 if group is None else group)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def authenticated(path, headers):
    """Reload the digest per request so rotation/revocation needs no restart."""
    if len(headers) != 1:
        return False
    match = re.fullmatch(r'Bearer ([A-Za-z0-9_-]{43})', headers[0], re.IGNORECASE)
    if match is None:
        return False
    try:
        record = json.loads(read_regular(path, 4096, {0, os.geteuid()}, forbidden=0o037))
        digest = record['digest']
        if record.get('version') != 1 or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            return False
        return hmac.compare_digest(hashlib.sha256(match[1].encode('ascii')).hexdigest(), digest)
    except (OSError, ValueError, KeyError, TypeError):
        return False
