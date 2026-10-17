"""Offline, immutable SQLite world bundles. A template is never a runnable world."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from contextlib import closing

IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')
FILES = {'world.mt', 'map.sqlite', 'map_meta.txt', 'env_meta.txt', 'mod_storage.sqlite'}


def regular(path, owners=None):
    path = Path(path)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Template paths cannot contain links')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Template files must be regular, independent files')
    if owners is not None and (info.st_uid not in owners or info.st_mode & 0o022):
        raise ValueError('Untrusted template file permissions')
    return info


def digest_file(path):
    regular(path)
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def settings(path):
    if regular(path).st_size > 65536:
        raise ValueError('Oversized world.mt')
    result = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError('Invalid world.mt')
        key, value = (part.strip() for part in line.split('=', 1))
        if key in result:
            raise ValueError('Duplicate world setting')
        result[key] = value
    if result.get('gameid') != 'minetest':
        raise ValueError('Unsupported game')
    for key in ('backend', 'player_backend', 'auth_backend', 'mod_storage_backend'):
        if result.get(key, 'sqlite3') != 'sqlite3':
            raise ValueError('Only SQLite world templates are supported')
    if result.get('load_mod_civicrm_bridge') != 'mods/civicrm_bridge':
        raise ValueError('Template must enable the shared bridge')
    return result


def inspect_content(path, owners=None):
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError('Invalid template directory')
    if owners is not None and os.name == 'posix':
        try:
            from .controller import trusted_world_directory
        except ImportError:
            from controller import trusted_world_directory
        with trusted_world_directory(path, owners, metadata_only=True):
            pass
    names = {entry.name for entry in path.iterdir()}
    if not {'world.mt', 'map.sqlite'} <= names or names - FILES - {'manifest.json'}:
        raise ValueError('Only clean world content is allowed; remove accounts, players, queues and logs')
    hashes = {}
    for name in sorted(names & FILES):
        regular(path / name, owners)
        hashes[name] = digest_file(path / name)
    settings(path / 'world.mt')
    for name in ('map.sqlite', 'mod_storage.sqlite'):
        if name not in names:
            continue
        with closing(sqlite3.connect((path / name).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('Invalid SQLite template content')
            if name == 'mod_storage.sqlite':
                if db.execute('SELECT 1 FROM entries WHERE modname=? LIMIT 1', ('civicrm_bridge',)).fetchone():
                    raise ValueError('Template contains bridge account state')
    return hashes


def revision(manifest):
    content = {key: manifest[key] for key in ('template_id', 'name', 'files')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def inspect_bundle(path, owners=None):
    path = Path(path)
    if regular(path / 'manifest.json', owners).st_size > 65536:
        raise ValueError('Oversized template manifest')
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if (set(manifest) != {'version', 'template_id', 'name', 'files', 'revision'}
            or type(manifest['version']) is not int or manifest['version'] != 1
            or not isinstance(manifest['template_id'], str) or not IDENTIFIER.fullmatch(manifest['template_id'])
            or not isinstance(manifest['name'], str) or not 1 <= len(manifest['name'].strip()) <= 255
            or manifest['files'] != inspect_content(path, owners) or manifest['revision'] != revision(manifest)):
        raise ValueError('Invalid or modified published template')
    return manifest


def catalogue(config):
    result = []
    templates = config.get('templates', {})
    if not isinstance(templates, dict) or len(templates) > 1000:
        raise ValueError('Invalid template configuration')
    for identifier, path in sorted(templates.items()):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError('Invalid configured template')
        manifest = inspect_bundle(path, {0, config['world_owner_uid']})
        if manifest['template_id'] != identifier:
            raise ValueError('Template ID mismatch')
        result.append({key: manifest[key] for key in ('template_id', 'name', 'revision')})
    return {'version': 1, 'templates': result}
