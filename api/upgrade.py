#!/usr/bin/env python3
"""Operator activation of private games, preserving the existing API identity/state."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.security import atomic_private, check_parents, read_regular
from lifecycle.templates import inspect_bundle

LIB = Path('/usr/local/lib/civicrm-bridge-api')
CONTROLLER = Path('/etc/civicrm-worldctl/config.json')
CONFIG = Path('/etc/civicrm-bridge-api/config.json')
MANIFEST = Path('/var/lib/civicrm-worldctl/install-manifest.json')
PUBLISHER = Path('/usr/local/lib/civicrm-worldctl/publish_template.py')
SERVICE = 'civicrm-bridge-api.service'
DROPIN = Path('/etc/systemd/system/civicrm-bridge-api.service.d')
SUDOERS = Path('/etc/sudoers.d/civicrm-bridge-private-games')
TEMPLATES = {name: '/home/ben/luanti-dev/templates/' + name + '-v1' for name in ('dev', 'test')}
CODE = {
    'api': ('__init__.py', 'security.py', 'contract.py', 'tokens.py', 'jobs.py', 'worlds.py', 'app.py', 'serve.py'),
    'lifecycle': ('__init__.py', 'controller.py', 'errors.py', 'docker_backend.py', 'instances.py', 'instance_contract.py', 'templates.py'),
    'tools': ('world_worker.py',),
}


class CommandFailed(RuntimeError):
    def __init__(self, step, returncode, stderr=b''):
        self.step = step
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(step + ' failed (exit ' + str(returncode) + ')')


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def registration(controller_raw, api_raw, manifest_raw, publisher):
    """Build configuration changes without replacing token, state or other settings."""
    controller, api, manifest = json.loads(controller_raw), json.loads(api_raw), json.loads(manifest_raw)
    if manifest['sha256'].get(str(CONTROLLER)) != hashlib.sha256(controller_raw).hexdigest():
        raise ValueError('Controller configuration was modified outside its managed installation')
    for config in (controller, api):
        templates = config.setdefault('templates', {})
        for identifier, path in TEMPLATES.items():
            if identifier in templates and templates[identifier] != path:
                raise ValueError('Refusing to replace a different registered template')
            templates[identifier] = path
    for key in ('instances_root', 'instance_registry'):
        if key in api and api[key] != controller[key]:
            raise ValueError('API and controller instance paths disagree')
        api[key] = controller[key]
    controller_raw = encoded(controller)
    manifest['sha256'][str(CONTROLLER)] = hashlib.sha256(controller_raw).hexdigest()
    manifest['sha256'][str(PUBLISHER)] = hashlib.sha256(publisher).hexdigest()
    return controller_raw, encoded(api), encoded(manifest)


def command(*arguments):
    program = Path(arguments[0]).name
    step = ('API-user catalogue/import verification' if program == 'runuser' else
            program + (' ' + arguments[1] if program in ('systemctl', 'systemd-analyze') else ''))
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=660)
    if result.returncode:
        raise CommandFailed(step, result.returncode, result.stderr)
    return result.stdout


def trusted(path):
    return read_regular(path, 4 * 1024 * 1024, {0})


def directory(path):
    check_parents(path)
    if not path.exists():
        path.mkdir(mode=0o755)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Expected a root-owned installation directory')
    path.chmod(0o755)


def restore_files(originals):
    for path, original in reversed(list(originals.items())):
        if original is None:
            path.unlink(missing_ok=True)
        else:
            atomic_private(path, original[0], original[1], owner=0, group=0)


def controller_lock():
    descriptor = os.open('/run/civicrm-worldctl.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        os.close(descriptor)
        raise ValueError('Unsafe controller lock')
    return os.fdopen(descriptor, 'r+')


def main():
    if os.geteuid() != 0:
        raise ValueError('Run the reviewed upgrade with sudo from the ben session')
    os.umask(0o077)
    directory(MANIFEST.parent)
    with controller_lock() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        controller_raw, api_raw, manifest_raw = trusted(CONTROLLER), trusted(CONFIG), trusted(MANIFEST)
        controller, api, manifest = json.loads(controller_raw), json.loads(api_raw), json.loads(manifest_raw)
        if manifest['sha256'].get(str(CONTROLLER)) != hashlib.sha256(controller_raw).hexdigest():
            raise ValueError('Controller configuration has unrecognized changes')
        if manifest['sha256'].get(str(PUBLISHER)) != hashlib.sha256(trusted(PUBLISHER)).hexdigest():
            raise ValueError('Installed publisher has unrecognized changes')
        if api['world_owner_uid'] != pwd.getpwnam('ben').pw_uid:
            raise ValueError('Unexpected API world owner')
        if api.get('state_dir') != '/var/lib/civicrm-bridge-api' or api.get('token_file') != '/etc/civicrm-bridge-api/token.json':
            raise ValueError('Review custom API state/token paths separately')
        if trusted(LIB / 'api/requirements.txt') != (ROOT / 'api/requirements.txt').read_bytes():
            raise ValueError('Dependency changes require separate review')
        template_files = []
        for identifier, value in TEMPLATES.items():
            path = Path(value)
            bundle = inspect_bundle(path, {0, api['world_owner_uid']})
            if bundle['template_id'] != identifier:
                raise ValueError('Published template ID mismatch')
            template_files.extend([path, *sorted(path.iterdir())])
        publisher = (ROOT / 'tools/publish_template.py').read_bytes()
        compile(publisher, 'publish_template.py', 'exec')
        new_controller, new_api, new_manifest = registration(controller_raw, api_raw, manifest_raw, publisher)
        updates = {CONTROLLER: (new_controller, 0o644), CONFIG: (new_api, 0o644),
                   PUBLISHER: (publisher, 0o644), MANIFEST: (new_manifest, 0o644)}
        for package, names in CODE.items():
            directory(LIB / package)
            for name in names:
                content = (ROOT / package / name).read_bytes()
                compile(content, package + '/' + name, 'exec')
                updates[LIB / package / name] = (content, 0o644)
        directory(DROPIN)
        updates[DROPIN / 'private-games.conf'] = (b'[Service]\nReadWritePaths=/home/ben/luanti-dev/instances\n', 0o644)
        sudoers = SUDOERS
        updates[sudoers] = (b'civicrm_bridge_api ALL=(root) NOPASSWD: /usr/local/sbin/civicrm-worldctl instance, /usr/local/sbin/civicrm-worldctl reconcile_instances\n', 0o440)
        with tempfile.NamedTemporaryFile() as candidate:
            candidate.write(updates[sudoers][0])
            candidate.flush()
            command('/usr/sbin/visudo', '-cf', candidate.name)
        originals = {}
        for path in updates:
            check_parents(path)
            originals[path] = (trusted(path), stat.S_IMODE(path.stat().st_mode)) if path.exists() or path.is_symlink() else None
        backup = MANIFEST.parent / ('private-games-backup-' + time.strftime('%Y%m%dT%H%M%S') + '-' + str(os.getpid()))
        backup.mkdir(mode=0o700)
        for index, (path, original) in enumerate(originals.items()):
            if original is not None:
                (backup / str(index)).write_bytes(original[0])
        (backup / 'files.json').write_bytes(encoded({str(path): {'index': index, 'mode': original[1]} if original else None
                                                   for index, (path, original) in enumerate(originals.items())}))
        acl_backup = backup / 'templates.acl'
        acl_backup.write_bytes(command('/usr/bin/getfacl', '-p', *map(str, template_files)))
        print('Preflight passed. Backup: ' + str(backup), flush=True)
        active = command('/usr/bin/systemctl', 'show', SERVICE, '--property=ActiveState', '--value').strip() == b'active'
        # Stop outside the controller lock so an in-flight lifecycle worker can finish.
        fcntl.flock(lock, fcntl.LOCK_UN)
        command('/usr/bin/systemctl', 'stop', SERVICE)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if trusted(CONTROLLER) != controller_raw or trusted(MANIFEST) != manifest_raw or trusted(CONFIG) != api_raw:
            if active:
                command('/usr/bin/systemctl', 'start', SERVICE)
            raise ValueError('Configuration changed during API shutdown; inspect before retrying')
        try:
            print('Installing API snapshot, template registrations and read permissions.', flush=True)
            for path, (content, mode) in updates.items():
                atomic_private(path, content, mode, owner=0, group=0)
            for path in template_files:
                if path.is_dir():
                    command('/usr/bin/setfacl', '-b', '-k', str(path))
                    path.chmod(0o555)
                else:
                    command('/usr/bin/setfacl', '-b', str(path))
                    path.chmod(0o444)
            command('/usr/sbin/visudo', '-cf', '/etc/sudoers')
            command('/usr/bin/systemctl', 'daemon-reload')
            command('/usr/bin/systemd-analyze', 'verify', SERVICE)
            probe = ('import json,sys; sys.path.insert(0,"/usr/local/lib/civicrm-bridge-api"); '
                     'from lifecycle.templates import catalogue; '
                     'c=json.load(open("/etc/civicrm-bridge-api/config.json")); '
                     'assert {x["template_id"] for x in catalogue(c)["templates"]} >= {"dev","test"}; '
                     'from api.app import create_app')
            command('/usr/sbin/runuser', '-u', 'civicrm_bridge_api', '--', str(LIB / 'venv/bin/python'), '-I', '-c', probe)
            if active:
                command('/usr/bin/systemctl', 'start', SERVICE)
        except Exception as error:
            if isinstance(error, CommandFailed):
                (backup / 'failed-step.json').write_bytes(encoded({'step': error.step, 'returncode': error.returncode}))
                (backup / 'failed-step.stderr').write_bytes(error.stderr)
                print('Failed step: ' + str(error) + '. Restoring previous installation.', flush=True)
            restore_files(originals)
            command('/usr/bin/setfacl', '--restore=' + str(acl_backup))
            command('/usr/bin/systemctl', 'daemon-reload')
            if active:
                command('/usr/bin/systemctl', 'start', SERVICE)
            raise
        print('Registered dev and test templates in the controller and API. API snapshot upgraded.')
        print('Template read access verified as civicrm_bridge_api. Token and job state retained; game service untouched.')
        print('API restarted.' if active else 'API was stopped and remains stopped.')
        print('Backup: ' + str(backup))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        reason = str(error) if isinstance(error, CommandFailed) else type(error).__name__
        print('Private-game activation failed (' + reason + '); inspect service status and retained backups.', file=sys.stderr)
        sys.exit(1)
