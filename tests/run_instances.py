#!/usr/bin/env python3
"""Disposable Linux instance factory/scheduler tests; never controls the live service."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.publish_template import publish
from lifecycle.templates import inspect_bundle, regular
if os.name == 'posix':
    from lifecycle.instances import InstanceController, runtime_for, registry
    from lifecycle.controller import LifecycleError
    from tools import world_worker


def fixture_json(path):
    regular(Path(path), {os.geteuid()})
    return json.loads(Path(path).read_text())


class Backend:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.active, self.path, self.starts, self.stops = False, None, 0, 0

    def info(self):
        return {'active': self.active, 'world_path': self.path, 'ready': self.active, 'pid': 123 if self.active else 0}

    def start(self, world_id=None, world_path=None):
        if self.active:
            raise AssertionError('Second engine process')
        identifier = json.loads(self.state.read_text())['world_id']
        self.path = str(Path(self.config['instances_root']) / identifier)
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False
        self.stops += 1


@unittest.skipUnless(os.name == 'posix', 'Linux filesystem and lifecycle controls required')
class InstanceTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.base = Path(self.temporary.name)
        self.addCleanup(self.cleanup)
        patched = patch('lifecycle.instances.trusted_json', side_effect=fixture_json)
        patched.start()
        self.addCleanup(patched.stop)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'world.mt').write_text('gameid = minetest\nbackend = sqlite3\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
        with closing(sqlite3.connect(self.source / 'map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
            db.execute("INSERT INTO blocks VALUES (1, 'clean')")
            db.commit()
        self.manifest = publish(self.source, self.base / 'template', 'classroom', 'Classroom')
        for name in ('instances', 'registry', 'home/.minetest/mods/civicrm_bridge'):
            (self.base / name).mkdir(parents=True)
        (self.base / 'home/.minetest/mods/civicrm_bridge/mod.conf').write_text('name = civicrm_bridge\n')
        (self.base / 'server.conf').write_text('bind_address = 127.0.0.1\n')
        self.state = self.base / 'state.json'
        self.state.write_text(json.dumps({'world_id': 'dev'}))
        self.config = {'worlds': {'dev': str(self.base / 'legacy')}, 'templates': {'classroom': str(self.base / 'template')},
            'instances_root': str(self.base / 'instances'), 'instance_registry': str(self.base / 'registry'),
            'user_home': str(self.base / 'home'), 'engine': sys.executable, 'game_id': 'minetest',
            'server_config': str(self.base / 'server.conf'), 'world_owner_uid': os.geteuid()}
        self.backend = Backend(self.config, self.state)
        self.clock = 100
        self.controller = InstanceController(self.config, self.backend, self.state, self.base / 'lock',
                                             clock=lambda: self.clock, root_owner=os.geteuid())

    def cleanup(self):
        for root, _, files in os.walk(self.base):
            Path(root).chmod(0o700)
            for name in files:
                if not (Path(root) / name).is_symlink():
                    (Path(root) / name).chmod(0o600)
        self.temporary.cleanup()

    def request(self, operation, number=1, **extra):
        return {'version': 2, 'job_id': operation + str(number), 'operation': operation,
                'instance_id': 'g' + str(number).zfill(26), **extra}

    def create(self, number=1):
        request = self.request('create_instance', number, template_id='classroom', template_revision=self.manifest['revision'],
                               access_window={'starts_at': 90, 'ends_at': 110})
        self.controller.handle(request)
        return request

    def configure(self, number=1, **extra):
        return self.controller.handle(self.request('configure_instance', number, access_window={'starts_at': 90, 'ends_at': 110},
                                                   generation=1, enabled=True, **extra))

    def test_copies_and_retries_preserve_template_and_independent_maps(self):
        request = self.create()
        self.create(2)
        self.controller.handle(request)
        first = Path(self.config['instances_root']) / request['instance_id']
        with closing(sqlite3.connect(first / 'map.sqlite')) as db:
            db.execute("UPDATE blocks SET data='organization one'")
            db.commit()
        second = Path(self.config['instances_root']) / self.request('instance_status', 2)['instance_id']
        with closing(sqlite3.connect(second / 'map.sqlite')) as db:
            self.assertEqual(db.execute('SELECT data FROM blocks').fetchone(), ('clean',))
        self.assertEqual(inspect_bundle(self.base / 'template')['revision'], self.manifest['revision'])
        self.assertEqual(len(registry(self.config)), 2)

    def test_only_one_reserved_game_starts_and_expiry_stops_without_portal(self):
        self.create()
        self.create(2)
        self.configure()
        with self.assertRaises(LifecycleError):
            self.configure(2)
        self.controller.reconcile()
        self.controller.reconcile()
        self.assertEqual(self.backend.starts, 1)
        self.clock = 110
        self.controller.reconcile()
        self.assertFalse(self.backend.active)
        self.assertEqual(len(registry(self.config)), 2)
        with self.assertRaises(LifecycleError):
            self.controller.handle(self.request('start_instance'))

    def test_archive_releases_server_but_retains_save_and_cannot_be_reactivated(self):
        self.create()
        self.configure()
        self.controller.reconcile()
        self.controller.handle(self.request('archive_instance'))
        self.assertFalse(self.backend.active)
        with self.assertRaises(LifecycleError):
            self.configure()
        with self.assertRaises(ValueError):
            runtime_for(self.config, self.request('instance_status')['instance_id'])
        self.assertTrue(Path(runtime_for(self.config, self.request('instance_status')['instance_id'], allow_archived=True)).is_dir())
        self.create(2)
        self.configure(2)
        self.controller.reconcile()
        self.assertEqual(self.backend.starts, 2)

    def test_published_copy_recovers_after_registry_write_failure_without_overwriting_progress(self):
        original = self.controller.save
        with patch.object(self.controller, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.create()
        destination = Path(self.config['instances_root']) / self.request('instance_status')['instance_id']
        (destination / 'players.sqlite').write_bytes(b'preserve progress')
        self.create()
        self.assertEqual((destination / 'players.sqlite').read_bytes(), b'preserve progress')
        self.assertEqual(len(registry(self.config)), 1)

    def test_worker_replays_an_interrupted_v2_operation_with_the_same_identity(self):
        request = self.create()
        raw = json.dumps(request).encode()
        calls = []
        def runner(arguments, body):
            calls.append(body)
            return self.controller.handle(json.loads(body))
        result = world_worker.handle(raw, self.base / 'worker', runner)
        self.assertTrue(result['ok'])
        self.assertEqual(world_worker.handle(raw, self.base / 'worker', runner), result)
        self.assertEqual(calls, [raw])
        self.assertEqual(len(registry(self.config)), 1)

    def test_failed_copy_resumes_staging_without_allocating_a_second_identity(self):
        with patch('lifecycle.instances.shutil.copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(registry(self.config), {})
        self.create()
        self.assertEqual(len(registry(self.config)), 1)

    def test_busy_controller_cannot_create_another_copy(self):
        with self.controller.lifecycle._lock():
            with self.assertRaises(LifecycleError) as error:
                self.create()
        self.assertEqual(error.exception.code, 'busy')
        self.assertEqual(registry(self.config), {})

    def test_inbox_inherits_read_access_for_a_different_service_identity(self):
        import pwd
        import subprocess
        from types import SimpleNamespace
        self.create()
        world = Path(self.config['instances_root']) / self.request('instance_status')['instance_id']
        self.controller.config['api_user'] = pwd.getpwuid(os.geteuid()).pw_name
        self.controller.owner = SimpleNamespace(st_uid=pwd.getpwnam('nobody').pw_uid)
        self.controller.permissions(world)
        job = world / 'civicrm_bridge/inbox/fixture.json'
        descriptor = os.open(job, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        os.close(descriptor)
        acl = subprocess.check_output(['/usr/bin/getfacl', '-n', '-c', '-p', str(job)], text=True)
        self.assertIn(f'user:{self.controller.owner.st_uid}:rwx', acl)


def real_engine(engine, game):
    os.umask(0o077)
    sys.path.insert(0, str(ROOT / 'tests'))
    from run_lifecycle import create_fixture, EngineBackend
    artifacts, config, state, lock = create_fixture(Path.home(), engine, game)
    backend = EngineBackend(config, state, artifacts)
    try:
        clean = artifacts / 'clean'
        clean.mkdir()
        shutil.copyfile(Path(config['worlds']['dev']) / 'world.mt', clean / 'world.mt')
        with closing(sqlite3.connect(clean / 'map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
        manifest = publish(clean, artifacts / 'published', 'classroom', 'Classroom')
        for name in ('instances', 'registry'):
            (artifacts / name).mkdir()
        config.update(templates={'classroom': str(artifacts / 'published')}, instances_root=str(artifacts / 'instances'),
                      instance_registry=str(artifacts / 'registry'))
        with patch('lifecycle.instances.trusted_json', side_effect=fixture_json):
            controller = InstanceController(config, backend, state, lock, root_owner=os.geteuid())
            backend.config = controller.lifecycle.config
            for number in (1, 2):
                identifier = 'g' + str(number).zfill(26)
                window = {'starts_at': 1, 'ends_at': 2**31}
                controller.handle({'version': 2, 'job_id': 'create'+str(number), 'operation': 'create_instance', 'instance_id': identifier,
                                   'template_id': 'classroom', 'template_revision': manifest['revision'], 'access_window': window})
                (Path(config['instances_root']) / identifier / 'test-control').mkdir()
                controller.handle({'version': 2, 'job_id': 'configure'+str(number), 'operation': 'configure_instance', 'instance_id': identifier,
                                   'access_window': window, 'generation': 1, 'enabled': True})
                controller.reconcile()
                snapshot = backend.import_account('account'+str(number), 'same_username', True)
                assert snapshot['exists'] and snapshot['denial'] is False, snapshot
                controller.handle({'version': 2, 'job_id': 'archive'+str(number), 'operation': 'archive_instance', 'instance_id': identifier})
            assert inspect_bundle(artifacts / 'published')['revision'] == manifest['revision']
        print('PASS: real engine creates isolated accounts in two private copies; template remains unchanged')
    finally:
        backend.stop()
        for root, _, files in os.walk(artifacts):
            Path(root).chmod(0o700)
            for filename in files:
                if not (Path(root) / filename).is_symlink():
                    (Path(root) / filename).chmod(0o600)
        shutil.rmtree(artifacts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-engine', action='store_true')
    parser.add_argument('--engine', default='/usr/bin/luanti')
    parser.add_argument('--game-path', type=Path, default=Path('/home/ben/.minetest/games/minetest_game'))
    args = parser.parse_args()
    if args.real_engine:
        real_engine(args.engine, args.game_path)
    else:
        unittest.main(argv=[sys.argv[0]], verbosity=2)
