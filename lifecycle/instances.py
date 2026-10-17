"""Private world factory and schedule enforcement behind the restricted controller."""
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
import time

try:
    from .controller import LifecycleController, LifecycleError, trusted_world_directory, trusted_json
    from .instance_contract import ID, validate
    from .templates import inspect_bundle, digest_file, regular
except ImportError:
    from controller import LifecycleController, LifecycleError, trusted_world_directory, trusted_json
    from instance_contract import ID, validate
    from templates import inspect_bundle, digest_file, regular


def registry(config):
    root = Path(config['instance_registry'])
    if root.resolve() != root or not root.is_dir():
        raise ValueError('Invalid instance registry')
    result = {}
    for path in sorted(root.glob('*.json')):
        if not ID.fullmatch(path.stem):
            raise ValueError('Invalid registered identity')
        data = trusted_json(path)
        if data.get('instance_id') != path.stem:
            raise ValueError('Invalid registry record')
        result[path.stem] = data
    return result


def managed_config(config):
    result = {**config, 'worlds': dict(config.get('worlds', {}))}
    for identifier in registry(config):
        result['worlds'][identifier] = str(Path(config['instances_root']) / identifier)
    return result


def allowed(record, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    return (record['enabled'] and not record['archived']
            and record['access_window']['starts_at'] <= timestamp < record['access_window']['ends_at'])


def runtime_for(config, identifier, allow_archived=False):
    if not ID.fullmatch(identifier):
        raise ValueError('Accounts must target a private game instance')
    record = registry(config).get(identifier)
    if record is None or (record['archived'] and not allow_archived):
        raise ValueError('Unknown or archived instance')
    return str(Path(config['instances_root']) / identifier / 'civicrm_bridge')


def atomic_record(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.instance-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class InstanceController:
    def __init__(self, config, backend, state_path, lock_path, clock=time.time, root_owner=0):
        self.config = config
        self.clock = clock
        self.root = Path(config['instances_root'])
        self.registry_root = Path(config['instance_registry'])
        self.owner = Path(config['user_home']).stat()
        self.owners = {0, self.owner.st_uid}
        for path in (self.root, self.registry_root):
            with trusted_world_directory(path, self.owners):
                pass
            if path.stat().st_uid != root_owner:
                raise ValueError('Managed instance roots must be root-owned')
        self.lifecycle = LifecycleController(managed_config(config), backend, state_path, lock_path)

    def save(self, record):
        atomic_record(self.registry_root / (record['instance_id'] + '.json'), record)

    def reload(self):
        self.lifecycle.config['worlds'] = managed_config(self.config)['worlds']

    def create(self, request):
        identifier = request['instance_id']
        origin = {key: request[key] for key in ('instance_id', 'template_id', 'template_revision', 'access_window')}
        existing = registry(self.config).get(identifier)
        if existing is not None:
            if existing['creation'] != origin:
                raise LifecycleError('instance_conflict', 'This identity already belongs to another copy.')
            return existing
        template_path = self.config.get('templates', {}).get(request['template_id'])
        candidates = ([template_path] if template_path else []) + self.config.get('template_revisions', {}).get(request['template_id'], [])
        if not candidates:
            raise LifecycleError('unknown_template', 'Template is not published.')
        template, manifest = None, None
        for candidate in candidates:
            inspected = inspect_bundle(Path(candidate), self.owners)
            if inspected['template_id'] == request['template_id'] and inspected['revision'] == request['template_revision']:
                template, manifest = Path(candidate), inspected
                break
        if manifest is None:
            raise LifecycleError('template_changed', 'The requested immutable template revision is unavailable.')
        destination = self.root / identifier
        stage = self.root / ('.prepare-' + identifier)
        if destination.exists():
            with trusted_world_directory(destination, self.owners):
                pass
            regular(destination / '.origin.json', {self.registry_root.stat().st_uid})
            if json.loads((destination / '.origin.json').read_text()) != origin:
                raise LifecycleError('instance_conflict', 'Existing save cannot be overwritten.')
        else:
            stage.mkdir(mode=0o700, exist_ok=True)
            with trusted_world_directory(stage, self.owners):
                pass
            for filename, expected in manifest['files'].items():
                target = stage / filename
                if target.exists() or target.is_symlink():
                    regular(target, self.owners)
                source_descriptor = os.open(template / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(source_descriptor, 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError('Invalid template file')
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                    with os.fdopen(descriptor, 'wb') as output:
                        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                            raise ValueError('Invalid staging file')
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                if digest_file(target) != expected:
                    raise ValueError('Template content changed while copying')
                os.chown(target, self.owner.st_uid, self.owner.st_gid)
            atomic_record(stage / '.origin.json', origin)
            runtime = stage / 'civicrm_bridge'
            runtime.mkdir(mode=0o700, exist_ok=True)
            with trusted_world_directory(runtime, self.owners):
                pass
            for name in ('inbox', 'outbox', 'receipts'):
                (runtime / name).mkdir(mode=0o700, exist_ok=True)
                with trusted_world_directory(runtime / name, self.owners):
                    pass
                os.chown(runtime / name, self.owner.st_uid, self.owner.st_gid)
            os.chown(runtime, self.owner.st_uid, self.owner.st_gid)
            os.chown(stage, self.owner.st_uid, self.owner.st_gid)
            self.permissions(stage)
            sync(stage)
            os.rename(stage, destination)
            sync(self.root)
        record = {'version': 1, 'instance_id': identifier, 'creation': origin, 'generation': 0,
                  'access_window': request['access_window'], 'enabled': False, 'archived': False}
        self.save(record)
        self.reload()
        return record

    def permissions(self, world):
        api_user = self.config.get('api_user')
        if api_user is None:
            return
        identity = pwd.getpwnam(api_user).pw_uid
        runtime = world / 'civicrm_bridge'
        for path in (world, runtime):
            subprocess.run(['/usr/bin/setfacl', '-m', f'u:{identity}:--x', str(path)], check=True, capture_output=True)
        for name, permission in (('inbox', 'rwx'), ('outbox', 'r-x')):
            subprocess.run(['/usr/bin/setfacl', '-m', f'u:{identity}:{permission},d:u:{identity}:{permission},d:u:{self.owner.st_uid}:rwx,d:g::---,d:o::---,d:m::rwx',
                            str(runtime / name)], check=True, capture_output=True)

    def state(self, record):
        status = self.lifecycle._status()
        if record['archived']:
            return 'archived'
        if status['active'] and status['world_id'] == record['instance_id']:
            return status['state']
        return 'stopped'

    def stop(self, identifier):
        status = self.lifecycle._status()
        if status['active'] and status['world_id'] == identifier:
            self.lifecycle.backend.stop()
            self.lifecycle._wait_stopped()

    def handle(self, request):
        validate(request)
        with self.lifecycle._lock():
            self.reload()
            identifier, operation = request['instance_id'], request['operation']
            record = self.create(request) if operation == 'create_instance' else registry(self.config).get(identifier)
            if record is None:
                raise LifecycleError('unknown_instance', 'Private game copy does not exist.')
            if operation == 'configure_instance':
                if record['archived']:
                    raise LifecycleError('instance_archived', 'Archived saves cannot be reactivated.')
                desired = {key: request[key] for key in ('generation', 'access_window', 'enabled')}
                if request['generation'] < record['generation'] or (request['generation'] == record['generation'] and
                        any(record[key] != desired[key] for key in desired)):
                    raise LifecycleError('stale_generation', 'A newer schedule is already registered.')
                window = request['access_window']
                for other_id, other in registry(self.config).items():
                    if other_id != identifier and request['enabled'] and other['enabled'] and not other['archived']:
                        if other['access_window']['starts_at'] < window['ends_at'] and other['access_window']['ends_at'] > window['starts_at']:
                            raise LifecycleError('schedule_conflict', 'This server already has a reservation.')
                record.update(desired)
                self.save(record)
                if not allowed(record, self.clock()):
                    self.stop(identifier)
            elif operation == 'start_instance':
                if not allowed(record, self.clock()):
                    raise LifecycleError('outside_approval', 'This game is outside its approved access period.')
                self.lifecycle._change(identifier, False)
            elif operation == 'stop_instance':
                self.stop(identifier)
            elif operation == 'archive_instance':
                record.update(enabled=False, archived=True)
                self.save(record)
                self.stop(identifier)
            return {'ok': True, 'instance_id': identifier, 'state': self.state(record)}

    def reconcile(self):
        with self.lifecycle._lock():
            self.reload()
            records = registry(self.config)
            status = self.lifecycle._status()
            running = records.get(status['world_id'])
            if running is not None and not allowed(running, self.clock()):
                self.stop(running['instance_id'])
            due = [record for record in records.values() if allowed(record, self.clock())]
            if len(due) > 1:
                raise LifecycleError('schedule_conflict', 'Multiple private games have an active reservation.')
            if due:
                self.lifecycle._change(due[0]['instance_id'], False)
            return {'ok': True}
