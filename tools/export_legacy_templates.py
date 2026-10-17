#!/usr/bin/env python3
"""Operator-only export of the dev/test maps, without accounts or player state."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing


CONTENT = ('world.mt', 'map_meta.txt', 'env_meta.txt')


def copy_regular(source, destination):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as content:
        info = os.fstat(content.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('World content must be independent regular files')
        with destination.open('xb') as target:
            shutil.copyfileobj(content, target)


def clean_source(source, destination):
    source = Path(source).absolute()
    if source.resolve() != source or not source.is_dir():
        raise ValueError('World paths cannot contain links')
    for name in CONTENT:
        path = source / name
        if name == 'world.mt' or path.exists() or path.is_symlink():
            copy_regular(path, destination / name)
    map_path = source / 'map.sqlite'
    info = map_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Map must be an independent regular SQLite file')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(map_path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            info = sidecar.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe SQLite sidecar')
    with closing(sqlite3.connect(map_path.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(destination / 'map.sqlite')) as target:
            original.backup(target)


def export_maps(controller, template_root, publish, inspect_bundle):
    template_root = Path(template_root)
    if template_root.resolve() != template_root or not template_root.is_dir():
        raise ValueError('Install the controller first; a safe template directory is required')
    results = []
    with controller._lock():
        initial = controller._status()
        if initial['active'] and initial['world_id'] not in ('dev', 'test'):
            raise ValueError('Only the legacy dev/test service may be interrupted')
        pending = []
        for identifier in ('dev', 'test'):
            destination = template_root / (identifier + '-v1')
            if destination.exists() or destination.is_symlink():
                manifest = inspect_bundle(destination)
                if manifest['template_id'] != identifier:
                    raise ValueError('Existing template has a different identity')
                results.append({'template_id': identifier, 'path': str(destination), 'reused': True})
            else:
                controller.validate_world(identifier)
                pending.append((identifier, destination))
        if not pending:
            return results
        try:
            if initial['active']:
                controller.backend.stop()
            controller._wait_stopped()
            for identifier, destination in pending:
                with tempfile.TemporaryDirectory(prefix='.legacy-export-', dir=template_root) as temporary:
                    stage = Path(temporary)
                    clean_source(Path(controller.config['worlds'][identifier]), stage)
                    manifest = publish(stage, destination, identifier, identifier.capitalize())
                results.append({'template_id': identifier, 'path': str(destination),
                                'revision': manifest['revision'], 'reused': False})
        finally:
            if initial['active']:
                controller._wait_stopped()
                controller.backend.start()
                controller._wait_ready(initial['world_id'])
    return results


def main():
    if os.geteuid() != 0:
        raise ValueError('Run this reviewed export from the ben session with sudo')
    installed = Path('/usr/local/lib/civicrm-worldctl')
    if not (installed / 'publish_template.py').is_file():
        raise ValueError('First run the reviewed lifecycle/install.py as ben with sudo')
    sys.path.insert(0, str(installed))
    from controller import LifecycleController, SystemdBackend, trusted_json
    from publish_template import publish
    from templates import inspect_bundle
    config = trusted_json(Path('/etc/civicrm-worldctl/config.json'))
    trusted_json(Path('/var/lib/civicrm-worldctl/state.json'))
    controller = LifecycleController(config, SystemdBackend(config),
                                     Path('/var/lib/civicrm-worldctl/state.json'),
                                     Path('/run/civicrm-worldctl.lock'))
    os.umask(0o077)
    results = export_maps(controller, Path('/home/ben/luanti-dev/templates'), publish, inspect_bundle)
    print(json.dumps({'templates': results}, indent=2))
    print('Original saves retained. Accounts, player databases, mod storage and bridge runtime were excluded.')
    print('Next register these template paths in the controller and API configurations; no configuration was changed.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Export failed ({type(error).__name__}). Inspect controller status before retrying; existing templates are retained.', file=sys.stderr)
        sys.exit(1)
