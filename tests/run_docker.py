#!/usr/bin/env python3
"""Portable Docker backend contracts. No daemon, Linux account or live world required."""
import copy
import io
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lifecycle.docker_backend import DockerBackend, validate_configuration, run_docker, MAX_OUTPUT, WORLD, ENTRYPOINT
from lifecycle.errors import LifecycleError
from lifecycle.install_docker import configuration

IMAGE = 'sha256:' + 'a' * 64
CONTAINER = 'b' * 64
START = '2026-10-17T12:00:00.123456789Z'


def fixture():
    return {'backend': 'docker', 'game_id': 'minetest', 'docker': {
        'installation_id': '11111111-1111-4111-8111-111111111111', 'image_id': IMAGE,
        'container_name': 'bridge-test', 'uid': 30000, 'gid': 30000, 'port': 30000,
        'bind_address': '127.0.0.1', 'home_path': '/deployment/home', 'mod_path': '/deployment/mod',
        'game_path': '/deployment/game', 'config_path': '/deployment/config', 'record_path': '/private/launch.json'}}


class MemoryRecord:
    def __init__(self):
        self.value = None

    def load(self):
        return copy.deepcopy(self.value)

    def save(self, value):
        self.value = copy.deepcopy(value)


class Docker:
    def __init__(self):
        self.remote = None
        self.calls = []
        self.logs = '[civicrm_bridge] Ready; local JSON inbox enabled\nServer for gameid="minetest" listening on 0.0.0.0:30000.\n'
        self.timeout_after_create = False
        self.timeout_after_start = False

    def __call__(self, arguments, **options):
        self.calls.append(list(arguments))
        if arguments[:2] == ['container', 'ls']:
            return CONTAINER + '\n' if self.remote else ''
        if arguments[:2] == ['container', 'inspect']:
            return json.dumps([self.remote])
        if arguments[0] == 'logs':
            return self.logs
        if arguments[0] == 'create':
            if self.remote:
                raise AssertionError('Unexpected second container')
            flags = {key: value for value in arguments for key, separator, value in [value.partition('=')] if separator}
            labels, mounts = {}, []
            for index, value in enumerate(arguments):
                if value == '--label':
                    name, content = arguments[index + 1].split('=', 1)
                    labels[name] = content
                if value == '--mount':
                    fields = dict(part.split('=', 1) if '=' in part else (part, True) for part in arguments[index + 1].split(','))
                    mounts.append({'Type': 'bind', 'Source': fields['src'], 'Destination': fields['dst'],
                                   'RW': 'readonly' not in fields, 'Propagation': 'rprivate'})
            self.remote = {'Id': CONTAINER, 'Name': '/' + arguments[2], 'Image': IMAGE,
                'Config': {'Labels': labels, 'User': flags['--user'], 'Env': ['HOME=/var/lib/minetest'], 'Entrypoint': [ENTRYPOINT],
                           'Cmd': arguments[arguments.index(IMAGE) + 1:]},
                'Mounts': mounts, 'State': {'Running': False, 'Pid': 0, 'StartedAt': START},
                'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapAdd': None, 'CapDrop': ['ALL'],
                    'SecurityOpt': ['no-new-privileges'], 'RestartPolicy': {'Name': 'no'}, 'NetworkMode': 'bridge',
                    'PidMode': '', 'UsernsMode': '', 'IpcMode': 'private', 'PidsLimit': 256,
                    'Tmpfs': {'/tmp': 'rw,noexec,nosuid,size=64m'},
                    'LogConfig': {'Type': 'local', 'Config': {'max-size': '10m', 'max-file': '3'}},
                    'PortBindings': {'30000/udp': [{'HostIp': '127.0.0.1', 'HostPort': '30000'}]}}}
            if self.timeout_after_create:
                raise LifecycleError('outcome_unknown', 'Client timed out')
            return CONTAINER
        if arguments[:2] == ['container', 'start']:
            self.remote['State'].update(Running=True, Pid=123)
            if self.timeout_after_start:
                raise LifecycleError('outcome_unknown', 'Client timed out')
            return CONTAINER
        if arguments[:2] == ['container', 'stop']:
            self.remote['State'].update(Running=False, Pid=0)
            return CONTAINER
        if arguments[:2] == ['container', 'rm']:
            if self.remote['State']['Running']:
                raise AssertionError('Removing a running container')
            self.remote = None
            return CONTAINER
        raise AssertionError('Unexpected Docker invocation: ' + repr(arguments))


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.docker, self.store = Docker(), MemoryRecord()
        self.backend = DockerBackend(fixture(), runner=self.docker, store=self.store)

    def test_empty_installation_is_idle_without_a_selected_world(self):
        self.assertEqual({'active': False, 'pid': 0, 'world_path': None, 'ready': False, 'transitioning': False}, self.backend.info())
        self.assertIsNone(self.store.value)

    def test_daemon_failure_preserves_the_last_launch_record_without_retrying_a_mutation(self):
        self.backend.start('world-one', '/private/world-one')
        original = copy.deepcopy(self.store.value)
        unavailable = Mock(side_effect=LifecycleError('docker_unavailable', 'Daemon unavailable'))
        self.backend.run = unavailable
        for action in (self.backend.info, self.backend.stop, lambda: self.backend.start('world-two', '/private/world-two')):
            with self.assertRaises(LifecycleError):
                action()
            self.assertEqual(original, self.store.value)
        self.assertTrue(all(call.args[0][:2] == ['container', 'ls'] for call in unavailable.call_args_list))

    def test_docker_execution_bounds_output_and_preserves_ambiguous_timeout_outcomes(self):
        for mutation in (False, True):
            for problem in ('timeout', 'oversized'):
                with self.subTest(mutation=mutation, problem=problem):
                    process = Mock(returncode=0)
                    process.stdout = io.BytesIO(b'x' * (MAX_OUTPUT + 1) if problem == 'oversized' else b'')
                    process.stderr = io.BytesIO(b'private diagnostic text')
                    if problem == 'timeout':
                        process.wait.side_effect = [subprocess.TimeoutExpired('docker', 1), 0]
                    with patch('lifecycle.docker_backend.subprocess.Popen', return_value=process), self.assertRaises(LifecycleError) as failure:
                        run_docker(['info'], timeout=1, mutation=mutation)
                    process.kill.assert_called()
                    self.assertEqual('outcome_unknown' if mutation else 'docker_unavailable', failure.exception.code)
                    self.assertNotIn('private diagnostic text', str(failure.exception))

    def test_start_uses_immutable_image_and_only_selected_world_is_writable(self):
        self.backend.start('world-one', '/private/world-one')
        status = self.backend.info()
        self.assertTrue(status['ready'])
        self.assertEqual('/private/world-one', status['world_path'])
        self.assertEqual(CONTAINER, self.store.value['container_id'])
        create = next(call for call in self.docker.calls if call[0] == 'create')
        for flag in ('--pull=never', '--read-only', '--restart=no', '--cap-drop=ALL', '--security-opt=no-new-privileges'):
            self.assertIn(flag, create)
        self.assertIn(IMAGE, create)
        writable = [item['Source'] for item in self.docker.remote['Mounts'] if item['RW']]
        self.assertEqual(['/private/world-one'], writable)
        self.assertEqual(5, len(self.docker.remote['Mounts']))
        self.assertNotIn('/var/run/docker.sock', json.dumps(self.docker.remote['Mounts']))

    def test_missing_either_readiness_message_never_reports_ready(self):
        self.backend.start('world-one', '/private/world-one')
        for logs in ('', '[civicrm_bridge] Ready;', 'Server for gameid="minetest" listening on 0.0.0.0:30000.'):
            with self.subTest(logs=logs):
                self.docker.logs = logs
                self.assertFalse(self.backend.info()['ready'])

    def test_readiness_cache_is_bound_to_the_current_container_start(self):
        self.backend.start('world-one', '/private/world-one')
        self.assertTrue(self.backend.info()['ready'])
        self.docker.remote['State']['StartedAt'] = '2026-10-17T12:01:00.000000000Z'
        self.docker.logs = ''
        self.assertFalse(self.backend.info()['ready'])
        self.assertEqual('2026-10-17T12:01:00.000000000Z', self.docker.calls[-1][2])

    def test_same_world_start_is_idempotent_and_different_world_requires_stop(self):
        self.backend.start('world-one', '/private/world-one')
        self.backend.start('world-one', '/private/world-one')
        with self.assertRaises(LifecycleError) as failure:
            self.backend.start('world-two', '/private/world-two')
        self.assertEqual('already_running', failure.exception.code)
        self.assertEqual(1, sum(call[0] == 'create' for call in self.docker.calls))

    def test_stopping_then_switching_never_removes_world_data_or_volumes(self):
        self.backend.start('world-one', '/private/world-one')
        self.backend.stop()
        self.backend.start('world-two', '/private/world-two')
        self.assertEqual('/private/world-two', self.backend.info()['world_path'])
        removals = [call for call in self.docker.calls if call[:2] == ['container', 'rm']]
        self.assertEqual([['container', 'rm', CONTAINER]], removals)
        self.assertFalse(any(call[0] in ('volume', 'system', 'image') for call in self.docker.calls))

    def test_ambiguous_create_or_start_recovers_by_inspection_without_duplicate_container(self):
        self.docker.timeout_after_create = self.docker.timeout_after_start = True
        self.backend.start('world-one', '/private/world-one')
        self.assertTrue(self.backend.info()['ready'])
        self.assertEqual(1, sum(call[0] == 'create' for call in self.docker.calls))

    def test_foreign_or_modified_container_is_never_stopped_or_removed(self):
        self.backend.start('world-one', '/private/world-one')
        original = copy.deepcopy(self.docker.remote)
        changes = [
            lambda value: value.update(Id='c' * 64),
            lambda value: value.update(Image='sha256:' + 'c' * 64),
            lambda value: value['Config']['Labels'].update({'bridge-installation': 'other'}),
            lambda value: value['Config'].update(User='0'),
            lambda value: value['Config'].update(Env=['HOME=/var/lib/minetest', 'LD_PRELOAD=/unexpected']),
            lambda value: value['HostConfig'].update(GroupAdd=['0']),
            lambda value: value['Config'].update(Cmd=['--world', '/other']),
            lambda value: value['Mounts'][0].update(RW=True),
            lambda value: value['Mounts'].append({'Type': 'volume', 'Source': 'extra', 'Destination': '/extra', 'RW': True}),
            lambda value: value['HostConfig'].update(Privileged=True),
            lambda value: value['HostConfig'].update(ReadonlyRootfs=False),
            lambda value: value['HostConfig'].update(CapAdd=['SYS_ADMIN']),
            lambda value: value['HostConfig'].update(SecurityOpt=[]),
            lambda value: value['HostConfig'].update(PidMode='host'),
            lambda value: value['HostConfig'].update(NetworkMode='host'),
            lambda value: value['HostConfig'].update(PortBindings={}),
            lambda value: value['HostConfig'].update(RestartPolicy={'Name': 'always'}),
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.docker.remote = copy.deepcopy(original)
                change(self.docker.remote)
                before = len(self.docker.calls)
                with self.assertRaises(LifecycleError):
                    self.backend.stop()
                self.assertFalse(any(call[:2] in (['container', 'stop'], ['container', 'rm']) for call in self.docker.calls[before:]))

    def test_container_without_private_launch_record_is_not_adopted(self):
        self.backend.start('world-one', '/private/world-one')
        self.store.value = None
        with self.assertRaises(LifecycleError):
            self.backend.stop()

    def test_invalid_configuration_and_mount_option_injection_are_rejected(self):
        for field, value in [('uid', 0), ('gid', True), ('image_id', 'mutable:latest'), ('mod_path', '/mod,readonly=false'),
                             ('game_path', '/game/../other'), ('port', 0), ('bind_address', 'host.example')]:
            with self.subTest(field=field):
                config = fixture()
                config['docker'][field] = value
                with self.assertRaises(LifecycleError):
                    validate_configuration(config)
        with self.assertRaises(LifecycleError):
            self.backend.start('world', '/world,src=/etc')
        self.assertEqual([], self.docker.calls)

    def test_installer_configuration_is_portable_idle_and_has_no_native_host_assumptions(self):
        controller, api = configuration({'image': 'ghcr.io/luanti-org/luanti:5.17.0', 'game_source': '/supplied/game',
            'admin_name': 'test_admin', 'hostname': 'localhost', 'development': True}, IMAGE, '11111111-1111-4111-8111-111111111111')
        self.assertEqual({}, controller['worlds'])
        self.assertEqual({}, api['templates'])
        self.assertEqual('docker', api['backend'])
        self.assertNotIn('/home/ben', json.dumps([controller, api]))
        self.assertNotIn('engine', controller)


if __name__ == '__main__':
    unittest.main(verbosity=2)
