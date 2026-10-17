#!/usr/bin/env python3
"""Publish a clean, stopped SQLite world as a new immutable template bundle."""
import argparse
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import stat

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from lifecycle.templates import IDENTIFIER, inspect_content, inspect_bundle, revision
except ModuleNotFoundError:
    from templates import IDENTIFIER, inspect_content, inspect_bundle, revision


def publish(source, destination, template_id, name):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if not IDENTIFIER.fullmatch(template_id) or not 1 <= len(name.strip()) <= 255:
        raise ValueError('Invalid template identity')
    hashes = inspect_content(source)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Published templates cannot be overwritten')
    if destination.parent.resolve() != destination.parent:
        raise ValueError('Destination parents cannot contain links')
    with tempfile.TemporaryDirectory(prefix='.publish-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir(mode=0o700)
        if os.name == 'posix':
            for attribute in ('system.posix_acl_access', 'system.posix_acl_default'):
                try:
                    os.removexattr(stage, attribute)
                except OSError as error:
                    if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                        raise
        for filename in hashes:
            descriptor = os.open(source / filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as content, (stage / filename).open('xb') as target:
                metadata = os.fstat(content.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError('Source changed to an unsafe file')
                shutil.copyfileobj(content, target)
                target.flush()
                os.fsync(target.fileno())
            (stage / filename).chmod(0o444)
        manifest = {'version': 1, 'template_id': template_id, 'name': name.strip(), 'files': hashes}
        manifest['revision'] = revision(manifest)
        (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n', encoding='utf-8')
        (stage / 'manifest.json').chmod(0o444)
        inspect_bundle(stage)
        os.rename(stage, destination)
        destination.chmod(0o555)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--template-id', required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    try:
        result = publish(args.source, args.destination, args.template_id, args.name)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Template publication failed: {error}\n')
    print(json.dumps({key: result[key] for key in ('template_id', 'revision')}))


if __name__ == '__main__':
    main()
