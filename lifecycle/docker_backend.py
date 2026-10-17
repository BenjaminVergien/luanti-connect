"""A single, strictly owned Luanti container; no Docker privileges for the API."""
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid

try:
    from .errors import LifecycleError
except ImportError:
    from errors import LifecycleError

HOME = '/var/lib/minetest'
WORLD = HOME + '/.minetest/worlds/active'
ENTRYPOINT = '/bin/sh'
MAX_OUTPUT = 256 * 1024


def path_value(value):
    if (not isinstance(value, str) or not value.startswith('/')
            or any(item in value for item in ('\x00', '\n', '\r', ',', '\\'))
            or '..' in PurePosixPath(value).parts or str(PurePosixPath(value)) != value):
        raise LifecycleError('invalid_configuration', 'Docker paths must be canonical absolute Linux paths.')
    return value


def validate_configuration(config):
    docker = dict(config['docker'])
    if (not re.fullmatch(r'[0-9a-f-]{36}', docker.get('installation_id', ''))
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', docker.get('image_id', ''))
            or not re.fullmatch(r'[a-z][a-z0-9-]{1,62}', docker.get('container_name', ''))):
        raise LifecycleError('invalid_configuration', 'Invalid installed Docker identity or image.')
    for field in ('home_path', 'mod_path', 'game_path', 'config_path', 'record_path'):
        path_value(docker.get(field))
    for field in ('uid', 'gid'):
        if type(docker.get(field)) is not int or not 1 <= docker[field] < 2**31:
            raise LifecycleError('invalid_configuration', 'Luanti requires an explicit non-root identity.')
    environment = docker.get('image_environment', [])
    if not isinstance(environment, list) or any(not isinstance(value, str) or '=' not in value for value in environment):
        raise LifecycleError('invalid_configuration', 'Invalid immutable image environment.')
    if type(docker.get('port')) is not int or not 1 <= docker['port'] <= 65535:
        raise LifecycleError('invalid_configuration', 'Invalid Docker game port.')
    try:
        ipaddress.IPv4Address(docker['bind_address'])
    except (ValueError, KeyError):
        raise LifecycleError('invalid_configuration', 'Invalid Docker bind address.') from None
    if config.get('game_id') != 'minetest':
        raise LifecycleError('invalid_configuration', 'Docker mode currently supports Minetest Game.')
    return docker


def run_docker(arguments, *, timeout=10, mutation=False):
    """Bound both streams while reading; killing a client does not undo a daemon operation."""
    command = ['/usr/bin/docker', '--host=unix:///var/run/docker.sock', *arguments]
    chunks = [bytearray(), bytearray()]
    exceeded = threading.Event()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin',
                                   'HOME': '/root', 'LC_ALL': 'C'})

        def read(stream, output):
            try:
                while data := stream.read(8192):
                    if len(output) + len(data) > MAX_OUTPUT:
                        exceeded.set()
                        process.kill()
                        break
                    output.extend(data)
            finally:
                stream.close()

        readers = [threading.Thread(target=read, args=(stream, chunks[index]), daemon=True)
                   for index, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise LifecycleError('outcome_unknown' if mutation else 'docker_unavailable',
                                 'Docker timed out; inspect current status before retrying.') from None
        finally:
            for reader in readers:
                reader.join(timeout=2)
        if exceeded.is_set() or process.returncode:
            raise LifecycleError('outcome_unknown' if mutation else 'docker_unavailable',
                                 'Docker did not confirm the operation; inspect current status.')
        return bytes(chunks[0] + (chunks[1] if arguments[0] == 'logs' else bytearray())).decode('utf-8')
    except (OSError, UnicodeError):
        raise LifecycleError('outcome_unknown' if mutation else 'docker_unavailable',
                             'Cannot communicate with the local Docker daemon.') from None


class LaunchRecord:
    def __init__(self, path):
        self.path = Path(path)

    def check_parent(self):
        try:
            from .controller import trusted_world_directory
        except ImportError:
            from controller import trusted_world_directory
        with trusted_world_directory(self.path.parent, {0}):
            pass

    def load(self):
        self.check_parent()
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
                raise LifecycleError('unsafe_state', 'Untrusted Docker launch record.')
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise LifecycleError('unsafe_state', 'Oversized Docker launch record.')
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('version') != 1:
            raise LifecycleError('unsafe_state', 'Invalid Docker launch record.')
        return value

    def save(self, value):
        self.check_parent()
        self.load()
        descriptor, temporary = tempfile.mkstemp(prefix='.launch-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class DockerBackend:
    def __init__(self, config, runner=run_docker, store=None, clock=time.time):
        self.config = config
        self.docker = validate_configuration(config)
        self.run = runner
        self.store = store if store is not None else LaunchRecord(self.docker['record_path'])
        self.clock = clock

    def approved(self, world_id, world_path):
        if 'instance_registry' not in self.config:
            return
        try:
            from .instances import registry, allowed
        except ImportError:
            from instances import registry, allowed
        record = registry(self.config).get(world_id)
        if (record is None or not allowed(record, self.clock())
                or world_path != str(Path(self.config['instances_root']) / world_id)):
            raise LifecycleError('outside_approval', 'The private game no longer has an enabled reservation.')

    def validate_installation(self):
        try:
            from .controller import trusted_world_directory
        except ImportError:
            from controller import trusted_world_directory
        for field in ('home_path', 'mod_path', 'game_path', 'config_path'):
            root = Path(self.docker[field])
            with trusted_world_directory(root, {0}):
                pass
            for path in root.rglob('*'):
                info = path.lstat()
                if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        or info.st_uid != 0 or info.st_mode & 0o022
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                    raise LifecycleError('invalid_configuration', 'Docker code and configuration must be immutable reviewed files.')
        for field, filename in (('mod_path', 'mod.conf'), ('mod_path', 'init.lua'), ('mod_path', 'bridge.lua'),
                                ('game_path', 'game.conf'), ('config_path', 'minetest.conf')):
            if not (Path(self.docker[field]) / filename).is_file():
                raise LifecycleError('invalid_configuration', 'A required Docker deployment file is missing.')
        details = json.loads(self.run(['image', 'inspect', self.docker['image_id']]))
        if not isinstance(details, list) or len(details) != 1 or details[0].get('Id') != self.docker['image_id']:
            raise LifecycleError('invalid_configuration', 'The installed Luanti image is unavailable.')

    def mounts(self, world):
        d = self.docker
        return [(d['home_path'], HOME, False), (d['config_path'], '/etc/minetest', False),
                (d['mod_path'], HOME + '/.minetest/mods/civicrm_bridge', False),
                (d['game_path'], HOME + '/.minetest/games/minetest_game', False),
                (path_value(world), WORLD, True)]

    def labels(self, record):
        return {'managed-by': 'civicrm-bridge', 'bridge-installation': self.docker['installation_id'],
                'bridge-launch': record['launch_id'], 'bridge-world': record['world_id']}

    def engine_arguments(self):
        return ['-c', 'umask 0077; exec /usr/local/bin/luantiserver "$@"', 'luanti',
                '--gameid', 'minetest', '--world', WORLD, '--config',
                '/etc/minetest/minetest.conf', '--logfile', '']

    def create_arguments(self, record):
        d = self.docker
        arguments = ['create', '--name', d['container_name'], '--pull=never', '--read-only',
                     '--restart=no', '--network=bridge', '--ipc=private', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--pids-limit=256',
                     '--tmpfs=/tmp:rw,noexec,nosuid,size=64m', '--log-driver=local',
                     '--log-opt=max-size=10m', '--log-opt=max-file=3',
                     '--user=' + str(d['uid']) + ':' + str(d['gid']),
                     '--env=HOME=' + HOME, '--entrypoint=' + ENTRYPOINT,
                     '--publish=' + d['bind_address'] + ':' + str(d['port']) + ':30000/udp']
        for name, value in self.labels(record).items():
            arguments += ['--label', name + '=' + value]
        for source, target, writable in self.mounts(record['world_path']):
            arguments += ['--mount', 'type=bind,src=' + source + ',dst=' + target + ('' if writable else ',readonly')]
        return [*arguments, d['image_id'], *self.engine_arguments()]

    def find(self):
        identifiers = self.run(['container', 'ls', '--all', '--no-trunc',
                                '--filter', 'name=^/' + self.docker['container_name'] + '$',
                                '--format', '{{.ID}}']).splitlines()
        if not identifiers:
            return None
        if len(identifiers) != 1 or not re.fullmatch('[0-9a-f]{64}', identifiers[0]):
            raise LifecycleError('unmanaged_instance', 'Container identity is ambiguous.')
        values = json.loads(self.run(['container', 'inspect', identifiers[0]]))
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise LifecycleError('docker_unavailable', 'Docker returned an invalid container inspection.')
        return values[0]

    def owned(self, remote, record):
        """A name or a label alone is never authority to stop/remove a container."""
        try:
            if (not record or not re.fullmatch('[0-9a-f-]{36}', record['launch_id'])
                    or not re.fullmatch('[A-Za-z0-9_-]{1,32}', record['world_id'])
                    or not re.fullmatch('[0-9a-f]{64}', remote['Id'])
                    or record.get('container_id') not in (None, remote['Id'])):
                return False
            d, host, config = self.docker, remote['HostConfig'], remote['Config']
            environment = [value for value in d.get('image_environment', []) if not value.startswith('HOME=')]
            environment.append('HOME=' + HOME)
            expected_mounts = sorted(self.mounts(record['world_path']))
            actual_mounts = sorted((item['Source'], item['Destination'], item['RW']) for item in remote['Mounts'] if item['Type'] == 'bind' and item.get('Propagation') == 'rprivate')
            labels = config.get('Labels') or {}
            return (remote['Name'] == '/' + d['container_name'] and remote['Image'] == d['image_id']
                    and all(labels.get(key) == value for key, value in self.labels(record).items())
                    and config['User'] == str(d['uid']) + ':' + str(d['gid'])
                    and config['Entrypoint'] == [ENTRYPOINT] and config['Cmd'] == self.engine_arguments()
                    and sorted(config.get('Env') or []) == sorted(environment)
                    and actual_mounts == expected_mounts and len(remote['Mounts']) == len(expected_mounts)
                    and host['ReadonlyRootfs'] is True and host['Privileged'] is False
                    and host.get('CapAdd') in (None, []) and host['CapDrop'] == ['ALL']
                    and host['SecurityOpt'] == ['no-new-privileges']
                    and host['RestartPolicy']['Name'] == 'no' and host['NetworkMode'] == 'bridge'
                    and host.get('PidMode', '') == '' and host.get('UsernsMode', '') == ''
                    and host.get('IpcMode') == 'private' and host.get('Devices') in (None, [])
                    and host.get('DeviceRequests') in (None, [])
                    and host.get('GroupAdd') in (None, [])
                    and host['PidsLimit'] == 256 and host['Tmpfs'] == {'/tmp': 'rw,noexec,nosuid,size=64m'}
                    and host['LogConfig'] == {'Type': 'local', 'Config': {'max-size': '10m', 'max-file': '3'}}
                    and host['PortBindings'] == {'30000/udp': [{'HostIp': d['bind_address'], 'HostPort': str(d['port'])}]})
        except (KeyError, TypeError, ValueError, LifecycleError):
            return False

    def inspect_owned(self):
        remote = self.find()
        record = self.store.load()
        if remote is None and record and record.get('container_id') is not None:
            identifier = record['container_id']
            if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
                raise LifecycleError('unsafe_state', 'Invalid recorded container identity.')
            existing = self.run(['container', 'ls', '--all', '--no-trunc', '--filter', 'id=' + identifier,
                                 '--format', '{{.ID}}']).splitlines()
            if existing:
                raise LifecycleError('unmanaged_instance', 'The recorded container exists under another name; refusing control.')
        if remote is not None:
            if not self.owned(remote, record):
                raise LifecycleError('unmanaged_instance', 'Container differs from its installed launch specification; refusing control.')
            if record.get('container_id') is None:
                record['container_id'] = remote['Id']
                self.store.save(record)
        return remote, record

    def info(self):
        remote, record = self.inspect_owned()
        if remote is None:
            return {'active': False, 'pid': 0, 'world_path': None, 'ready': False, 'transitioning': False}
        state = remote['State']
        if state.get('Paused') or state.get('Restarting'):
            raise LifecycleError('unmanaged_instance', 'Container is in an unexpected lifecycle state.')
        active = state['Running'] is True
        ready = False
        if active:
            started = state['StartedAt']
            if not isinstance(started, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z', started):
                raise LifecycleError('docker_unavailable', 'Invalid container start identity.')
            ready = record.get('ready_start') == started
            if not ready:
                logs = self.run(['logs', '--since', started, '--tail', '1000', remote['Id']])
                ready = ('[civicrm_bridge] Ready;' in logs and 'Server for gameid="minetest" listening on ' in logs)
                if ready:
                    current = self.find()
                    if current is None or not self.owned(current, record) or current['State']['StartedAt'] != started or not current['State']['Running']:
                        ready = False
                    else:
                        record['ready_start'] = started
                        self.store.save(record)
        return {'active': active, 'pid': state.get('Pid', 0) if active else 0,
                'world_path': record['world_path'] if active else None,
                'ready': ready, 'transitioning': False}

    def start(self, world_id, world_path):
        if not re.fullmatch('[A-Za-z0-9_-]{1,32}', world_id):
            raise LifecycleError('unknown_world', 'Invalid selected world.')
        path_value(world_path)
        self.approved(world_id, world_path)
        remote, record = self.inspect_owned()
        if remote is not None:
            if remote['State']['Running']:
                if record['world_id'] == world_id and record['world_path'] == world_path:
                    return
                raise LifecycleError('already_running', 'A different game is already running.')
            try:
                self.run(['container', 'rm', remote['Id']], mutation=True)
            except LifecycleError:
                fresh, _ = self.inspect_owned()
                if fresh is not None:
                    raise
        record = {'version': 1, 'launch_id': str(uuid.uuid4()), 'world_id': world_id,
                  'world_path': world_path, 'container_id': None, 'ready_start': None}
        self.store.save(record)
        self.approved(world_id, world_path)
        try:
            self.run(self.create_arguments(record), mutation=True)
        except LifecycleError:
            remote, _ = self.inspect_owned()
            if remote is None:
                raise
        remote, record = self.inspect_owned()
        if remote is None:
            raise LifecycleError('outcome_unknown', 'Container creation was not confirmed.')
        self.approved(world_id, world_path)
        try:
            self.run(['container', 'start', remote['Id']], mutation=True)
        except LifecycleError:
            fresh, _ = self.inspect_owned()
            if fresh is None or not fresh['State']['Running']:
                raise

    def stop(self):
        remote, _ = self.inspect_owned()
        if remote is None or not remote['State']['Running']:
            return
        try:
            self.run(['container', 'stop', '--time', '20', remote['Id']], timeout=25, mutation=True)
        except LifecycleError:
            fresh, _ = self.inspect_owned()
            if fresh is not None and fresh['State']['Running']:
                raise
