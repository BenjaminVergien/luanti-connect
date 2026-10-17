#!/usr/bin/env python3
"""Preflight or apply a reviewed Docker bridge installation on Ubuntu 24.04."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lifecycle.docker_backend import DockerBackend, path_value, run_docker
from lifecycle.errors import LifecycleError

LIB = Path('/usr/local/lib/civicrm-worldctl')
API_LIB = Path('/usr/local/lib/civicrm-bridge-api')
ETC = Path('/etc/civicrm-worldctl')
STATE = Path('/var/lib/civicrm-worldctl')
DATA = Path('/var/lib/civicrm-luanti')
API_ETC = Path('/etc/civicrm-bridge-api')
API_STATE = Path('/var/lib/civicrm-bridge-api')
SERVICE = 'civicrm-bridge-api.service'
RUNTIME = {
    'api': ('__init__.py', 'security.py', 'contract.py', 'tokens.py', 'jobs.py', 'app.py', 'serve.py'),
    'lifecycle': ('__init__.py', 'controller.py', 'errors.py', 'docker_backend.py', 'instances.py', 'instance_contract.py', 'templates.py'),
    'tools': ('world_worker.py',),
}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive'}


def configuration(settings, image_id, installation_id):
    fields = {'image', 'game_source', 'admin_name', 'hostname', 'templates', 'development', 'uid', 'gid', 'port', 'bind_address'}
    if not isinstance(settings, dict) or set(settings) - fields:
        raise ValueError('Unexpected Docker installation setting')
    if not re.fullmatch(r'ghcr\.io/luanti-org/luanti:(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})', settings.get('image', '')):
        raise ValueError('Supply an official Luanti image reference already present on this host')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', settings.get('admin_name', '')):
        raise ValueError('Supply a valid server administrator name')
    development = settings.get('development', False)
    if type(development) is not bool:
        raise ValueError('Invalid development setting')
    hostname = settings.get('hostname', '')
    if not (development and hostname == 'localhost'):
        from api.install import hostname as validate_hostname
        validate_hostname(hostname)
    path_value(settings.get('game_source'))
    templates = settings.get('templates', {})
    if not isinstance(templates, dict) or any(not re.fullmatch('[A-Za-z0-9_-]{1,32}', name) for name in templates):
        raise ValueError('Invalid supplied template catalogue')
    for path in templates.values():
        path_value(path)
    docker = {'installation_id': installation_id, 'image_id': image_id,
              'container_name': 'civicrm-luanti', 'uid': settings.get('uid', 30000), 'gid': settings.get('gid', 30000),
              'port': settings.get('port', 30000), 'bind_address': settings.get('bind_address', '0.0.0.0'),
              'home_path': (DATA / 'home').as_posix(), 'mod_path': (DATA / 'mod').as_posix(),
              'game_path': (DATA / 'game').as_posix(), 'config_path': (ETC / 'luanti').as_posix(),
              'record_path': (STATE / 'docker-launch.json').as_posix()}
    controller = {'backend': 'docker', 'docker': docker, 'game_id': 'minetest',
                  'user_home': (DATA / 'owner').as_posix(), 'world_owner_uid': docker['uid'],
                  'server_config': (ETC / 'luanti/minetest.conf').as_posix(), 'api_user': 'civicrm_bridge_api',
                  'instances_root': (DATA / 'instances').as_posix(), 'instance_registry': (STATE / 'instances').as_posix(),
                  'worlds': {}, 'templates': {name: (DATA / 'templates' / name).as_posix() for name in templates},
                  'template_revisions': {}}
    DockerBackend(controller)
    api = {'backend': 'docker', 'hostname': hostname, 'token_file': (API_ETC / 'token.json').as_posix(),
           'state_dir': API_STATE.as_posix(), 'world_owner_uid': docker['uid'], 'worlds': {},
           'instances_root': controller['instances_root'], 'instance_registry': controller['instance_registry'],
           'templates': dict(controller['templates'])}
    return controller, api


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def source_tree(directory):
    """Copy reviewed content, never links, hard links, special files or mutable code."""
    from lifecycle.controller import trusted_world_directory, check_trusted_permissions
    directory = Path(directory)
    with trusted_world_directory(directory, {0}):
        pass
    result, size = {}, 0
    for path in sorted(directory.rglob('*')):
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Supplied deployment content must be root-owned and not writable by other users')
        if stat.S_ISDIR(info.st_mode):
            with trusted_world_directory(path, {0}):
                pass
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 16 * 1024 * 1024:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                check_trusted_permissions(stream.fileno(), {0})
                raw = stream.read(16 * 1024 * 1024 + 1)
            size += len(raw)
            if size > 128 * 1024 * 1024 or len(raw) > 16 * 1024 * 1024:
                raise ValueError('Supplied game content exceeds installation limits')
            result[str(path.relative_to(directory))] = raw
        else:
            raise ValueError('Supplied deployment content contains an unsafe file')
    return result


def deployment_files(settings, controller, api):
    from api.install import unit
    from api.security import read_regular
    files = {}
    for package, names in RUNTIME.items():
        for name in names:
            content = read_regular(ROOT / package / name, 2 * 1024 * 1024, {0})
            compile(content, package + '/' + name, 'exec')
            files[API_LIB / package / name] = (content, 0o644)
    for name in (*RUNTIME['lifecycle'], 'worldctl.py', 'launch.py'):
        files[LIB / name] = (read_regular(ROOT / 'lifecycle' / name, 2 * 1024 * 1024, {0}), 0o644)
    files[LIB / 'publish_template.py'] = (read_regular(ROOT / 'tools/publish_template.py', 2 * 1024 * 1024, {0}), 0o644)
    files[API_LIB / 'api/requirements.txt'] = (read_regular(ROOT / 'api/requirements.txt', 65536, {0}), 0o644)
    for name in ('init.lua', 'bridge.lua', 'mod.conf'):
        files[DATA / 'mod' / name] = (read_regular(ROOT / name, 2 * 1024 * 1024, {0}), 0o644)
    for name, raw in source_tree(settings['game_source']).items():
        files[DATA / 'game' / name] = (raw, 0o644)
    if DATA / 'game/game.conf' not in files:
        raise ValueError('Supplied Minetest Game has no game.conf')
    from lifecycle.templates import inspect_bundle
    for name, source in settings.get('templates', {}).items():
        manifest = inspect_bundle(Path(source), {0})
        if manifest['template_id'] != name:
            raise ValueError('Supplied template ID does not match its manifest')
        for filename in ('manifest.json', *manifest['files']):
            files[Path(controller['templates'][name]) / filename] = ((Path(source) / filename).read_bytes(), 0o644)
    files[ETC / 'config.json'] = (encoded(controller), 0o644)
    files[API_ETC / 'config.json'] = (encoded(api), 0o644)
    server = ('name = ' + settings['admin_name'] + '\nport = 30000\nbind_address = 0.0.0.0\n'
              'enable_ipv6 = false\nserver_announce = false\ncreative_mode = false\nenable_damage = false\n')
    files[ETC / 'luanti/minetest.conf'] = (server.encode(), 0o644)
    files[Path('/usr/local/sbin/civicrm-worldctl')] = (b'#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/lib/civicrm-worldctl/worldctl.py "$@"\n', 0o755)
    commands = ('status', 'stop_world', 'instance', 'reconcile_instances')
    sudoers = 'civicrm_bridge_api ALL=(root) NOPASSWD: ' + ', '.join('/usr/local/sbin/civicrm-worldctl ' + command for command in commands) + '\n'
    files[Path('/etc/sudoers.d/civicrm-bridge-api')] = (sudoers.encode(), 0o440)
    service = unit(venv_path()).decode().replace('After=network.target systemd-tmpfiles-setup.service',
             'After=network.target systemd-tmpfiles-setup.service docker.service\nRequires=docker.service')
    service = service.replace('/home/ben/luanti-dev/instances', (DATA / 'instances').as_posix())
    files[Path('/etc/systemd/system') / SERVICE] = (service.encode(), 0o644)
    files[Path('/etc/tmpfiles.d/civicrm-bridge-api.conf')] = (b'f /run/civicrm-worldctl.lock 0600 root root -\n', 0o644)
    if settings.get('development'):
        files[Path('/usr/local/lib/civicrm-bridge-development-proxy.py')] = (read_regular(ROOT / 'api/development_proxy.py', 65536, {0}), 0o644)
        files[Path('/etc/systemd/system/civicrm-bridge-development-proxy.service')] = (b'''[Unit]
Description=Private development bridge HTTPS proxy
After=civicrm-bridge-api.service
Requires=civicrm-bridge-api.service
[Service]
User=civicrm_bridge_api
Group=civicrm_bridge_api
ExecStart=/usr/bin/python3 -I /usr/local/lib/civicrm-bridge-development-proxy.py
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
RestrictAddressFamilies=AF_INET AF_UNIX
[Install]
WantedBy=multi-user.target
''', 0o644)
    return files


def command(*arguments, timeout=120):
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=ENV, umask=0o022)


def venv_path():
    return API_LIB / ('venv-' + hashlib.sha256((ROOT / 'api/requirements.txt').read_bytes()).hexdigest())


def directory(path, mode=0o755):
    from lifecycle.controller import trusted_world_directory
    path = Path(path)
    if not path.exists():
        directory(path.parent)
        path.mkdir(mode=mode)
        path.chmod(mode)
    with trusted_world_directory(path, {0}):
        pass


def identities(uid, gid, apply=False):
    import pwd
    import grp
    try:
        owner = pwd.getpwnam('civicrm_luanti')
        if owner.pw_uid != uid or owner.pw_gid != gid or owner.pw_shell != '/usr/sbin/nologin':
            raise ValueError('Existing Luanti service identity conflicts with configuration')
    except KeyError:
        try:
            pwd.getpwuid(uid)
        except KeyError:
            pass
        else:
            raise ValueError('Configured Luanti UID is already in use')
        try:
            group = grp.getgrgid(gid)
            if group.gr_name != 'civicrm_luanti':
                raise ValueError('Configured Luanti GID is already in use')
        except KeyError:
            if apply:
                command('/usr/sbin/groupadd', '--system', '--gid', str(gid), 'civicrm_luanti')
        if apply:
            command('/usr/sbin/useradd', '--system', '--uid', str(uid), '--gid', str(gid), '--no-create-home',
                    '--home-dir', (DATA / 'owner').as_posix(), '--shell', '/usr/sbin/nologin', 'civicrm_luanti')
    try:
        api = pwd.getpwnam('civicrm_bridge_api')
        if api.pw_uid == 0 or api.pw_shell != '/usr/sbin/nologin':
            raise ValueError('Invalid existing API identity')
        try:
            docker_group = grp.getgrnam('docker').gr_gid
        except KeyError:
            docker_group = -1
        if docker_group in os.getgrouplist(api.pw_name, api.pw_gid):
            raise ValueError('The API identity must not have Docker group access')
    except KeyError:
        if apply:
            command('/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
                    '--home-dir', API_STATE.as_posix(), '--shell', '/usr/sbin/nologin', 'civicrm_bridge_api')


@contextmanager
def transaction(files, manifest_path, manifest):
    """Back up and restore managed files on failure; never roll back runtime state."""
    from api.security import atomic_private, read_regular
    backup = STATE / ('docker-backup-' + time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    directory(backup, 0o700)
    originals = {}
    for index, path in enumerate([*files, manifest_path]):
        original = (read_regular(path, 16 * 1024 * 1024, {0}), stat.S_IMODE(path.stat().st_mode)) if path.exists() or path.is_symlink() else None
        originals[path] = original
        if original:
            (backup / str(index)).write_bytes(original[0])
    (backup / 'files.json').write_bytes(encoded({str(path): {'index': index, 'mode': original[1]} if original else None
                                               for index, (path, original) in enumerate(originals.items())}))
    changed = []
    try:
        for path, (raw, mode) in files.items():
            directory(path.parent)
            atomic_private(path, raw, mode=mode, owner=0, group=0, existing_limit=16 * 1024 * 1024)
            changed.append(path)
        command('/usr/sbin/visudo', '-cf', '/etc/sudoers')
        command('/usr/bin/systemctl', 'daemon-reload')
        command('/usr/bin/systemd-analyze', 'verify', SERVICE)
        yield
        changed.append(manifest_path)
        atomic_private(manifest_path, encoded(manifest), mode=0o600, owner=0, group=0)
    except Exception:
        for path in reversed(changed):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_private(path, original[0], mode=original[1], owner=0, group=0, existing_limit=16 * 1024 * 1024)
        subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], env=ENV, check=False, capture_output=True)
        raise


def install(settings, apply=False):
    if os.name != 'posix' or os.geteuid() != 0:
        raise ValueError('Run Docker installation checks as root on the Ubuntu Docker host')
    from api.security import read_regular, atomic_private
    from lifecycle.controller import trusted_json
    import pwd
    import fcntl
    os.umask(0o077)
    operating_system = Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in operating_system or 'VERSION_ID="24.04"' not in operating_system:
        raise ValueError('Docker installation currently requires Ubuntu 24.04')
    for executable in ('/usr/bin/docker', '/usr/sbin/visudo', '/usr/bin/setfacl', '/usr/bin/getfacl',
                       '/usr/bin/python3', '/usr/bin/systemctl', '/usr/sbin/useradd', '/usr/sbin/groupadd'):
        if not Path(executable).is_file():
            raise ValueError('Required executable is missing: ' + executable)
    info = json.loads(run_docker(['info', '--format', '{{json .}}']))
    if any('rootless' in option or 'userns' in option for option in info.get('SecurityOptions', [])):
        raise ValueError('Docker mode requires the local rootful daemon without UID remapping')
    manifest_path = STATE / 'docker-install-manifest.json'
    previous = json.loads(read_regular(manifest_path, 2 * 1024 * 1024, {0}, forbidden=0o077)) if manifest_path.exists() else None
    if not previous and (ETC / 'config.json').exists():
        raise ValueError('An existing native or unrecognized installation cannot be replaced automatically')
    details = json.loads(run_docker(['image', 'inspect', settings['image']]))
    image_id = details[0]['Id']
    image = details[0]
    if image.get('Config', {}).get('Entrypoint') != ['/usr/local/bin/luantiserver']:
        raise ValueError('The supplied image has an unsupported engine entrypoint')
    probe = 'bridge-image-check-' + uuid.uuid4().hex
    try:
        version = run_docker(['run', '--name', probe, '--pull=never', '--network=none', '--read-only',
                              '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=30000:30000',
                              '--tmpfs=/var/lib/minetest:rw,nosuid,size=4m,uid=30000,gid=30000',
                              '--tmpfs=/etc/minetest:ro,nosuid,size=1m', image_id, '--version'], timeout=20, mutation=True)
        match = re.search(r'Luanti (\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (5, 17, 0):
            raise ValueError('The supplied image requires Luanti 5.17.0 or newer')
    finally:
        # The unguessable probe identity was created by this invocation only.
        subprocess.run(['/usr/bin/docker', '--host=unix:///var/run/docker.sock', 'rm', '-f', probe],
                       env=ENV, capture_output=True, timeout=15, check=False)
    installation_id = previous['installation_id'] if previous else str(uuid.uuid4())
    controller, api = configuration(settings, image_id, installation_id)
    controller['docker']['image_environment'] = image['Config'].get('Env') or []
    old_config = trusted_json(ETC / 'config.json') if previous else None
    from lifecycle.templates import inspect_bundle
    retained = dict(old_config.get('template_revisions', {})) if old_config else {}
    current_templates = dict(old_config.get('templates', {})) if old_config else {}
    for name, source in settings.get('templates', {}).items():
        template = inspect_bundle(Path(source), {0})
        if template['template_id'] != name:
            raise ValueError('Supplied template ID differs from its manifest')
        destination = str(DATA / 'templates' / name / template['revision'])
        if name in current_templates and current_templates[name] != destination:
            retained[name] = sorted(set([*retained.get(name, []), current_templates[name]]))
        current_templates[name] = destination
    controller.update(templates=current_templates, template_revisions=retained)
    api.update(templates=dict(current_templates))
    uid, gid = controller['docker']['uid'], controller['docker']['gid']
    identities(uid, gid)
    files = deployment_files(settings, controller, api)
    if previous:
        for name in previous['sha256']:
            path = Path(name)
            if path not in files and DATA / 'templates' in path.parents:
                files[path] = (read_regular(path, 16 * 1024 * 1024, {0}), 0o644)
    for path, (_, mode) in files.items():
        if path.exists() or path.is_symlink():
            if previous is None or str(path) not in previous['sha256']:
                raise ValueError('Refusing to overwrite an unrecognized deployment file: ' + str(path))
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise ValueError('An installed managed file has unrecognized permissions')
    hashes = {str(path): hashlib.sha256(raw).hexdigest() for path, (raw, _) in files.items()}
    if previous:
        for name, digest in previous['sha256'].items():
            if hashlib.sha256(read_regular(name, 16 * 1024 * 1024, {0})).hexdigest() != digest:
                raise ValueError('An installed managed file was modified outside the installer')
        if set(previous['sha256']) - set(hashes):
            raise ValueError('Removing previously installed game/template files requires separate review')
    changed = previous is None or previous['sha256'] != hashes
    backend = DockerBackend(controller)
    if previous and changed and DockerBackend(trusted_json(ETC / 'config.json')).info()['active']:
        raise ValueError('Stop the current game before changing installed code or configuration')
    result = {'version': 1, 'changed': changed, 'image_id': image_id, 'installation_id': installation_id, 'applied': False}
    if not apply:
        return result
    directory(STATE, 0o755)
    lock = os.open('/run/civicrm-worldctl.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(lock)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0 or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077:
            raise ValueError('Untrusted lifecycle lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if previous and not changed:
            return dict(result, applied=True)
        identities(uid, gid, apply=True)
        for path in (DATA, DATA / 'instances', DATA / 'templates', STATE / 'instances', API_ETC, API_LIB, LIB,
                     DATA / 'home/.minetest/mods/civicrm_bridge', DATA / 'home/.minetest/games/minetest_game',
                     DATA / 'home/.minetest/worlds/active', DATA / 'home/.minetest/mod_data'):
            directory(path)
        owner = DATA / 'owner'
        if not owner.exists():
            owner.mkdir(mode=0o700)
            os.chown(owner, uid, gid)
        if owner.is_symlink() or owner.stat().st_uid != uid or owner.stat().st_gid != gid or owner.stat().st_mode & 0o077:
            raise ValueError('Invalid Luanti ownership anchor')
        api_identity = pwd.getpwnam('civicrm_bridge_api')
        if not API_STATE.exists():
            API_STATE.mkdir(mode=0o700)
            os.chown(API_STATE, api_identity.pw_uid, api_identity.pw_gid)
        if API_STATE.is_symlink() or API_STATE.stat().st_uid != api_identity.pw_uid or API_STATE.stat().st_mode & 0o077:
            raise ValueError('Untrusted existing API state')
        venv = venv_path()
        requirements = files[API_LIB / 'api/requirements.txt'][0]
        requirements_path = API_LIB / 'requirements-install.txt'
        atomic_private(requirements_path, requirements, mode=0o600, owner=0, group=0)
        if not (venv / 'bin/python').exists():
            command('/usr/bin/python3', '-m', 'venv', str(venv))
        command(str(venv / 'bin/python'), '-I', '-m', 'pip', 'install', '--only-binary=:all:', '-r', str(requirements_path), timeout=300)
        requirements_path.unlink()
        was_active = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', SERVICE], env=ENV).returncode == 0
        if was_active:
            fcntl.flock(lock, fcntl.LOCK_UN)
            command('/usr/bin/systemctl', 'stop', SERVICE, timeout=630)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if previous and DockerBackend(trusted_json(ETC / 'config.json')).info()['active']:
                raise ValueError('A game started during installation; stop it before updating')
            if previous:
                retire_stopped(DockerBackend(trusted_json(ETC / 'config.json')))
            manifest = {'version': 1, 'installation_id': installation_id, 'sha256': hashes}
            with transaction(files, manifest_path, manifest):
                if not (STATE / 'state.json').exists():
                    atomic_private(STATE / 'state.json', encoded({'version': 1, 'world_id': None}), mode=0o644, owner=0, group=0)
                from api.tokens import generate, active
                token = API_ETC / 'token.json'
                delivery = API_ETC / 'development.token'
                if not token.exists():
                    generate(token, delivery, api_identity.pw_gid)
                elif not active(token):
                    raise ValueError('Existing API token is revoked or invalid; refusing implicit rotation')
                for template in controller['templates'].values():
                    command('/usr/bin/setfacl', '-R', '-m', 'u:' + str(api_identity.pw_uid) + ':r-X', template)
                backend.validate_installation()
        except Exception:
            if was_active:
                command('/usr/bin/systemctl', 'start', SERVICE)
            raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        command('/usr/bin/systemd-tmpfiles', '--create', '/etc/tmpfiles.d/civicrm-bridge-api.conf')
        command('/usr/bin/systemctl', 'enable', '--now', SERVICE)
        return dict(result, applied=True)
    finally:
        os.close(lock)


def retire_stopped(backend):
    """Validate using the old specification before an upgrade replaces it."""
    remote, _ = backend.inspect_owned()
    if remote is None:
        return
    if remote['State']['Running']:
        raise ValueError('Stop the managed game before changing its installation')
    try:
        backend.run(['container', 'rm', remote['Id']], mutation=True)
    except LifecycleError:
        current, _ = backend.inspect_owned()
        if current is not None:
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from api.security import read_regular
    settings = json.loads(read_regular(args.config, 65536, {0}))
    try:
        print(json.dumps(install(settings, args.apply)))
    except (LifecycleError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': 'Docker installation failed: ' + (str(error) if isinstance(error, (ValueError, LifecycleError)) else type(error).__name__)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
