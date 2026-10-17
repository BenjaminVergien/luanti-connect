#!/usr/bin/env python3
"""Portable source checks and deterministic, credential-free development bundles."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from .check_archive import sensitive_path
except ImportError:
    from check_archive import sensitive_path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {'api', 'lifecycle', 'tools', 'tests'}
TOP = {'init.lua', 'bridge.lua', 'mod.conf', 'README.md', 'LICENSE'}


def allowed(name):
    path = PurePosixPath(name)
    if ('\\' in name or path.is_absolute() or '..' in path.parts or str(path) != name
            or any(part.startswith('.') for part in path.parts) or sensitive_path(name)
            or path.name in {'token.json', 'token.lock'} or name.endswith('.token')):
        return False
    return (name in TOP or (2 <= len(path.parts) <= 4 and path.parts[0] == 'examples' and path.suffix == '.json')
            or (len(path.parts) == 2 and path.parts[0] in PACKAGES
            and (path.suffix == '.py' or name in {'tests/driver.lua', 'api/requirements.txt'})))


def source_files(root=ROOT):
    result = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if not allowed(name):
            continue
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Bundle contains a link or special file: ' + name)
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Source file exceeds bundle limit')
        result[name] = raw
    for required in ('init.lua', 'bridge.lua', 'lifecycle/install_docker.py', 'api/serve.py'):
        if required not in result:
            raise ValueError('Incomplete bridge source: ' + required)
    return result


def describe(root=ROOT):
    files = source_files(root)
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    git = ['git', '-c', 'safe.directory=' + str(root.resolve()), '-C', str(root)]
    head = subprocess.check_output([*git, 'rev-parse', 'HEAD'], timeout=15).decode().strip()
    status = subprocess.check_output([*git, 'status', '--porcelain', '--untracked-files=all'], timeout=15).decode().splitlines()
    return files, {'version': 1, 'sha256': digest, 'files': hashes, 'git_head': head, 'git_dirty': bool(status)}


def bundle(output, root=ROOT):
    files, manifest = describe(root)
    output = Path(output).resolve()
    if output == root.resolve() or root.resolve() in output.parents:
        raise ValueError('Store deployment bundles outside the bridge checkout')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in {**files, 'bundle.json': json.dumps(manifest, sort_keys=True).encode()}.items():
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, raw)
    return dict(manifest, archive_sha256=hashlib.sha256(output.read_bytes()).hexdigest())


def extract(archive_path, target, expected):
    """Root-side extraction verifies every member before writing any source."""
    raw = Path(archive_path).read_bytes()
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Development archive checksum mismatch')
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 256 or any(not allowed(name) and name != 'bundle.json' for name in names):
            raise ValueError('Unexpected development archive member')
        if any(info.file_size > 2 * 1024 * 1024 or info.external_attr >> 16 & 0o170000 != 0o100000 for info in archive.infolist()):
            raise ValueError('Unsafe development archive member')
        files = {name: archive.read(name) for name in names}
    manifest = json.loads(files.pop('bundle.json'))
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    if manifest.get('version') != 1 or manifest['files'] != hashes or manifest['sha256'] != hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
        raise ValueError('Development source manifest mismatch')
    target.mkdir(mode=0o700)
    for name, raw in files.items():
        path = target / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o644)
    (target / 'bundle.json').write_text(json.dumps(manifest, sort_keys=True))
    return manifest


def game_bundle(source, output):
    source = source.resolve(strict=True)
    if os.name == 'nt' and not str(source).startswith('\\\\?\\'):
        source = Path('\\\\?\\' + str(source))
    if source.is_symlink() or not (source / 'game.conf').is_file():
        raise ValueError('Supply a reviewed Minetest Game directory containing game.conf')
    size = 0
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            name = path.relative_to(source).as_posix()
            if any(part.startswith('.') for part in PurePosixPath(name).parts):
                continue
            if sensitive_path(name) or path.name in {'token.json', 'token.lock'} or name.endswith('.token'):
                raise ValueError('Supplied game content contains private configuration or runtime data')
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Supplied game content cannot contain links')
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.suffix in {'.key', '.pem', '.sqlite', '.db', '.log'}:
                raise ValueError('Supplied game content includes an unsafe file')
            size += info.st_size
            if info.st_size > 16 * 1024 * 1024 or size > 128 * 1024 * 1024:
                raise ValueError('Supplied game content exceeds its bound')
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    return {'archive_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def scan_sources():
    # Scan the exact pending snapshot, including uncommitted code, without
    # importing ignored local credentials into scanner input or reports.
    with tempfile.TemporaryDirectory(prefix='bridge-source-scan-') as name:
        target = Path(name)
        for path, raw in source_files().items():
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        result = subprocess.run(['gitleaks', 'dir', '--redact=100', '--no-banner', str(target)],
                                capture_output=True, timeout=120, check=False)
        if result.returncode:
            raise ValueError('Source scan failed; inspect a private redacted Gitleaks report before deployment')


def check():
    for name, raw in source_files().items():
        if name.endswith('.py'):
            compile(raw, name, 'exec')
    for suite in ('run_docker.py', 'run_development.py', 'run_archive_portable.py'):
        subprocess.run([sys.executable, '-B', str(ROOT / 'tests' / suite)], check=True)
    print('Portable checks passed. Linux ACL, systemd, API and engine suites require bridge test --server ID.', flush=True)
    subprocess.run([sys.executable, '-I', str(ROOT / 'tools/check_archive.py')], check=True)
    scan_sources()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('check', 'bundle', 'game-bundle'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if args.operation == 'check':
        check()
    else:
        if not args.output:
            parser.error('--output is required')
        print(json.dumps(game_bundle(args.source, args.output) if args.operation == 'game-bundle' else bundle(args.output)))


if __name__ == '__main__':
    main()
