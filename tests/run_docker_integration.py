#!/usr/bin/env python3
"""Real Docker/UID/ACL/lifecycle checks restricted to disposable per-run identities."""
import argparse
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lifecycle.controller import LifecycleController, LifecycleError
from lifecycle.docker_backend import DockerBackend, run_docker
from lifecycle.instances import InstanceController
from lifecycle.templates import inspect_bundle
from tools.publish_template import publish
from lifecycle.install_docker import retire_stopped

SETTINGS = None


class DockerIntegration(unittest.TestCase):
    def setUp(self):
        os.umask(0o022)
        self.root = Path(tempfile.mkdtemp(prefix='docker-', dir=SETTINGS.base))
        for name in ('home/.minetest/mods/civicrm_bridge', 'home/.minetest/games/minetest_game',
                     'home/.minetest/worlds/active', 'home/.minetest/mod_data', 'mod', 'config', 'owner', 'instances', 'registry', 'clean'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        os.chown(self.root / 'owner', 30000, 30000)
        for name in ('init.lua', 'bridge.lua', 'mod.conf'):
            shutil.copyfile(ROOT / name, self.root / 'mod' / name)
        (self.root / 'config/minetest.conf').write_text('name = Administrator\nbind_address = 0.0.0.0\nserver_announce = false\nenable_ipv6 = false\n')
        (self.root / 'clean/world.mt').write_text('gameid = minetest\nbackend = sqlite3\nauth_backend = sqlite3\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
        with closing(sqlite3.connect(self.root / 'clean/map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
        self.manifest = publish(self.root / 'clean', self.root / 'template', 'classroom', 'Docker test classroom')
        self.state = self.root / 'selection.json'
        self.state.write_text('{"version":1,"world_id":null}')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.config = {'backend': 'docker', 'game_id': 'minetest', 'worlds': {},
            'user_home': str(self.root / 'owner'), 'server_config': str(self.root / 'config/minetest.conf'),
            'templates': {'classroom': str(self.root / 'template')}, 'instances_root': str(self.root / 'instances'),
            'instance_registry': str(self.root / 'registry'), 'world_owner_uid': 30000,
            'docker': {'installation_id': '11111111-1111-4111-8111-111111111111', 'image_id': SETTINGS.image,
                'container_name': 'bridge-' + self.root.name.replace('_', '-'), 'uid': 30000, 'gid': 30000,
                'port': port, 'bind_address': '127.0.0.1', 'home_path': str(self.root / 'home'),
                'mod_path': str(self.root / 'mod'), 'game_path': str(SETTINGS.game_path),
                'config_path': str(self.root / 'config'), 'record_path': str(self.root / 'launch.json')}}
        self.config['docker']['image_environment'] = json.loads(run_docker(['image', 'inspect', SETTINGS.image]))[0]['Config'].get('Env') or []
        self.clock = 100
        self.backend = DockerBackend(self.config, clock=lambda: self.clock)
        self.controller = InstanceController(self.config, self.backend, self.state, self.root / 'lock', clock=lambda: self.clock)

    def tearDown(self):
        try:
            self.backend.stop()
            remote, _ = self.backend.inspect_owned()
            if remote:
                log = run_docker(['logs', '--tail', '50', remote['Id']])
                (self.root.parent / (self.root.name + '-engine.log')).write_text(log)
                run_docker(['container', 'rm', remote['Id']], mutation=True)
        finally:
            for root, _, files in os.walk(self.root):
                Path(root).chmod(0o700)
                for name in files:
                    (Path(root) / name).chmod(0o600)
            shutil.rmtree(self.root)

    def request(self, operation, number=1, **fields):
        return {'version': 2, 'job_id': operation + str(number), 'operation': operation,
                'instance_id': 'g' + str(number).zfill(26), **fields}

    def create(self, number=1):
        self.controller.handle(self.request('create_instance', number, template_id='classroom', template_revision=self.manifest['revision'],
                                           access_window={'starts_at': 90, 'ends_at': 110}))
        return self.root / 'instances' / ('g' + str(number).zfill(26))

    def configure(self, number=1):
        self.controller.handle(self.request('configure_instance', number, generation=1, enabled=True, access_window={'starts_at': 90, 'ends_at': 110}))

    def test_empty_installation_has_no_selected_world(self):
        self.backend.validate_installation()
        status = self.controller.lifecycle.status()
        self.assertFalse(status['active'])
        self.assertIsNone(status['selected_world_id'])

    def test_real_start_accounts_expiry_archive_and_save_retention(self):
        world, second = self.create(), self.create(2)
        self.configure()
        self.controller.reconcile()
        self.assertTrue(self.backend.info()['ready'])
        remote, record = self.backend.inspect_owned()
        self.assertEqual('30000:30000', remote['Config']['User'])
        self.assertEqual([str(world)], [item['Source'] for item in remote['Mounts'] if item['RW']])
        identifier = remote['Id']
        checks = run_docker(['exec', identifier, '/bin/sh', '-c',
            'test ! -S /var/run/docker.sock && test ! -e /etc/civicrm-bridge-api && '
            '! touch /var/lib/minetest/.write-probe && ! touch /etc/minetest/.write-probe && '
            '! touch /var/lib/minetest/.minetest/mods/civicrm_bridge/.write-probe && '
            'touch /var/lib/minetest/.minetest/worlds/active/save-probe'])
        self.assertTrue((world / 'save-probe').exists())
        raw = json.dumps({'version': 1, 'job_id': 'accounts-one', 'operation': 'sync_accounts', 'accounts': [
            {'username': 'docker_student', 'initial_password': 'FictitiousDockerTestOnly!', 'role': 'participant', 'admitted': True}]})
        inbox = world / 'civicrm_bridge/inbox'
        outbox = world / 'civicrm_bridge/outbox/accounts-one.json'
        (inbox / 'accounts-one.json').write_text(raw)
        os.chown(inbox / 'accounts-one.json', 30000, 30000)
        deadline = time.monotonic() + 15
        while not outbox.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual('ok', json.loads(outbox.read_text())['status'])
        self.clock = 110
        self.controller.reconcile()
        self.assertFalse(self.backend.info()['active'])
        self.assertTrue((world / 'auth.sqlite').exists())
        self.assertFalse((second / 'auth.sqlite').exists())
        self.assertEqual(self.manifest['revision'], inspect_bundle(self.root / 'template')['revision'])
        self.controller.handle(self.request('archive_instance'))
        self.assertTrue((world / 'save-probe').exists())
        with self.assertRaises(LifecycleError):
            self.controller.handle(self.request('start_instance'))

    def test_crash_reconciliation_replaces_only_its_container_and_keeps_save(self):
        world = self.create()
        self.configure()
        self.controller.reconcile()
        remote, _ = self.backend.inspect_owned()
        original = remote['Id']
        (world / 'retained-save').write_text('retained')
        run_docker(['kill', original], mutation=True)
        self.controller.reconcile()
        current, _ = self.backend.inspect_owned()
        self.assertNotEqual(original, current['Id'])
        self.assertTrue(self.backend.info()['ready'])
        self.assertEqual('retained', (world / 'retained-save').read_text())

    def test_failed_switch_rolls_back_to_the_previous_real_game(self):
        first, second = self.create(), self.create(2)
        self.configure()
        self.controller.reconcile()
        (first / 'retained-save').write_text('previous progress')
        (second / 'map.sqlite').write_bytes(b'Not a SQLite database')
        lifecycle = self.controller.lifecycle
        # This fixture exercises engine rollback independently of reservations.
        self.backend.config = {key: value for key, value in self.backend.config.items() if key != 'instance_registry'}
        lifecycle.timeout, lifecycle.poll_interval = 5, 0.1
        with self.assertRaises(LifecycleError) as failure:
            lifecycle.switch_world('g' + str(2).zfill(26))
        self.assertEqual('restored', failure.exception.details['rollback'])
        self.assertEqual(str(first), self.backend.info()['world_path'])
        self.assertTrue(self.backend.info()['ready'])
        self.assertEqual('previous progress', (first / 'retained-save').read_text())

    def test_container_name_collision_does_not_authorize_control(self):
        name = self.config['docker']['container_name']
        identifier = run_docker(['create', '--name', name, '--pull=never', '--label=bridge-test-run=' + self.root.name,
                                 '--tmpfs=/var/lib/minetest:rw,size=1m', '--tmpfs=/etc/minetest:ro,size=1m',
                                 SETTINGS.image, '--version'], mutation=True).strip()
        try:
            with self.assertRaises(LifecycleError) as failure:
                self.backend.stop()
            self.assertEqual('unmanaged_instance', failure.exception.code)
            remote = json.loads(run_docker(['inspect', identifier]))[0]
            self.assertEqual(identifier, remote['Id'])
            self.assertEqual(self.root.name, remote['Config']['Labels']['bridge-test-run'])
        finally:
            run_docker(['container', 'rm', identifier], mutation=True)

    def test_expired_reservation_is_rejected_at_the_docker_start_boundary(self):
        world = self.create()
        self.configure()
        self.clock = 110
        with self.assertRaises(LifecycleError) as failure:
            self.backend.start(world.name, str(world))
        self.assertEqual('outside_approval', failure.exception.code)
        self.assertIsNone(self.backend.find())
        self.assertIsNone(self.backend.store.load())

    def test_renamed_recorded_container_is_not_lost_or_replaced(self):
        world = self.create()
        self.configure()
        self.controller.reconcile()
        remote, _ = self.backend.inspect_owned()
        identifier = remote['Id']
        name = self.config['docker']['container_name']
        run_docker(['rename', identifier, name + '-renamed'], mutation=True)
        try:
            for action in (self.backend.info, self.backend.stop, lambda: self.backend.start(world.name, str(world))):
                with self.assertRaises(LifecycleError) as failure:
                    action()
                self.assertEqual('unmanaged_instance', failure.exception.code)
            self.assertTrue(json.loads(run_docker(['inspect', identifier]))[0]['State']['Running'])
        finally:
            run_docker(['rename', identifier, name], mutation=True)

    def test_upgrade_retires_the_old_stopped_specification_and_preserves_the_save(self):
        world = self.create()
        self.configure()
        self.controller.reconcile()
        original, _ = self.backend.inspect_owned()
        (world / 'upgrade-save').write_text('retained progress')
        self.backend.stop()
        changed = copy.deepcopy(self.config)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1', 0))
            changed['docker']['port'] = sock.getsockname()[1]
        if changed['docker']['port'] == self.config['docker']['port']:
            changed['docker']['port'] = self.config['docker']['port'] + 1
        replacement = DockerBackend(changed, clock=lambda: self.clock)
        with self.assertRaises(LifecycleError):
            replacement.info()
        retire_stopped(self.backend)
        self.backend = replacement
        self.backend.start(world.name, str(world))
        deadline = time.monotonic() + 10
        while not self.backend.info()['ready'] and time.monotonic() < deadline:
            time.sleep(0.1)
        current, _ = self.backend.inspect_owned()
        self.assertNotEqual(original['Id'], current['Id'])
        self.assertTrue(self.backend.info()['ready'])
        self.assertEqual(str(changed['docker']['port']), current['HostConfig']['PortBindings']['30000/udp'][0]['HostPort'])
        self.assertEqual('retained progress', (world / 'upgrade-save').read_text())

    def test_reservation_expiring_during_container_creation_never_starts_the_engine(self):
        world = self.create()
        self.configure()
        calls = []

        def command(arguments, **options):
            calls.append(arguments)
            result = run_docker(arguments, **options)
            if arguments[0] == 'create':
                self.clock = 110
            return result

        self.backend.run = command
        with self.assertRaises(LifecycleError):
            self.controller.reconcile()
        remote, _ = self.backend.inspect_owned()
        self.assertFalse(remote['State']['Running'])
        self.assertFalse(any(arguments[:2] == ['container', 'start'] for arguments in calls))
        self.assertFalse((world / 'auth.sqlite').exists())

    def test_modified_restart_policy_is_rejected_and_start_evidence_changes_on_restart(self):
        self.create()
        self.configure()
        self.controller.reconcile()
        remote, record = self.backend.inspect_owned()
        identifier, previous_start = remote['Id'], record['ready_start']
        try:
            run_docker(['update', '--restart=always', identifier], mutation=True)
            with self.assertRaises(LifecycleError):
                self.backend.stop()
        finally:
            run_docker(['update', '--restart=no', identifier], mutation=True)
        self.backend.stop()
        run_docker(['start', identifier], mutation=True)
        deadline = time.monotonic() + 10
        while not self.backend.info()['ready'] and time.monotonic() < deadline:
            time.sleep(0.1)
        current, record = self.backend.inspect_owned()
        self.assertNotEqual(previous_start, current['State']['StartedAt'])
        self.assertEqual(current['State']['StartedAt'], record['ready_start'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--game-path', required=True, type=Path)
    parser.add_argument('--base', required=True, type=Path)
    SETTINGS = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run on the isolated rootful Docker test host')
    unittest.main(argv=[sys.argv[0]], verbosity=2)
