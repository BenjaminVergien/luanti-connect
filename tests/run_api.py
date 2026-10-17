#!/usr/bin/env python3
"""Disposable API/token/queue/installer fixtures and optional real Caddy HTTPS."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
import httpx
import uvicorn
from api.app import create_app
from api.contract import validate_accounts
from api.install import deployment_files, hostname, unit
from api.jobs import Conflict, Jobs
from api.security import authenticated
from api import tokens
from tools import world_worker


def account(job_id='api-demo', admitted=True):
    return {'version': 1, 'job_id': job_id, 'operation': 'sync_accounts', 'accounts': [
        {'username': 'api_demo', 'initial_password': 'FictitiousApiOnly!',
         'role': 'participant', 'admitted': admitted}]}


def encoded(value):
    return json.dumps(value).encode()


def lifecycle(job_id='life-demo', operation='status', world=None):
    value = {'version': 1, 'job_id': job_id, 'operation': operation}
    if world is not None:
        value['world_id'] = world
    return encoded(value)


class Fixture(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix='api-tests-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = {'token_file': str(self.base / 'token.json'),
                       'state_dir': str(self.base / 'state'), 'world_owner_uid': os.geteuid(), 'worlds': {}}
        for name in ('dev', 'test'):
            runtime = self.base / name
            for area in ('inbox', 'outbox'):
                (runtime / area).mkdir(parents=True, mode=0o700)
            self.config['worlds'][name] = str(runtime)
        tokens.generate(self.base / 'token.json', self.base / 'delivery')
        self.secret = (self.base / 'delivery').read_text().strip()
        self.headers = {'Authorization': 'Bearer ' + self.secret, 'Content-Type': 'application/json'}
        self.calls = []
        # Existing transport fixtures use short aliases for synthetic private
        # instances. Filesystem registry validation is covered by run_instances.
        def resolve(config, identifier, allow_archived=False):
            if identifier not in config['worlds']:
                raise ValueError('Unknown private instance')
            return config['worlds'][identifier]
        for target in ('api.app.runtime_for', 'api.jobs.runtime_for'):
            mocked = patch(target, side_effect=resolve)
            mocked.start()
            self.addCleanup(mocked.stop)

    def runner(self, arguments):
        self.calls.append(arguments)
        return {'ok': True, 'ready': True, 'world_id': 'dev'}

    def app(self, background=False):
        return create_app(self.config, self.runner, background=background)

    def result(self, world, job_id='api-demo', status='ok', version=1):
        result = {'version': version, 'job_id': job_id, 'status': status,
                  'accounts': [{'username': 'api_demo', 'status': 'created'}]}
        runtime = Path(self.config['worlds'][world])
        (runtime / 'outbox' / (job_id + '.json')).write_bytes(encoded(result))
        (runtime / 'inbox' / (job_id + '.json')).unlink(missing_ok=True)
        return result


class ApiTests(Fixture):
    def test_v2_access_window_validation_and_result_version(self):
        value = account('timed-api')
        value['version'] = 2
        window = {'starts_at': 1800000000, 'ends_at': 1800000060}
        value['accounts'][0]['access_window'] = window
        raw = b'\n' + encoded(value) + b'\n'
        invalid_windows = [None, {}, {'starts_at': 1}, {'starts_at': True, 'ends_at': 2},
                           {'starts_at': 1.5, 'ends_at': 2}, {'starts_at': 2, 'ends_at': 2},
                           {'starts_at': 3, 'ends_at': 2}, {'starts_at': 1, 'ends_at': 2**53},
                           {**window, 'extra': True}]
        with TestClient(self.app()) as client:
            for bad_window in invalid_windows:
                bad = json.loads(encoded(value))
                bad['accounts'][0]['access_window'] = bad_window
                self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=encoded(bad),
                                             headers=self.headers).status_code, 400)
            missing = json.loads(encoded(value))
            del missing['accounts'][0]['access_window']
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=encoded(missing),
                                         headers=self.headers).status_code, 400)
            v1 = {**value, 'version': 1}
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=encoded(v1),
                                         headers=self.headers).status_code, 400)
            accepted = client.post('/v1/worlds/dev/accounts/jobs', content=raw, headers=self.headers)
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()['version'], 1)
            client.app.state.jobs.tick_accounts()
            self.assertEqual((self.base / 'dev/inbox/timed-api.json').read_bytes(), raw)
            expected = self.result('dev', 'timed-api', version=2)
            client.app.state.jobs.tick_accounts()
            completed = client.get(accepted.json()['result_url'], headers=self.headers).json()
            self.assertEqual(completed['version'], 1)
            self.assertEqual(completed['result'], expected)
            self.assertNotIn('initial_password', json.dumps(completed))
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=raw, headers=self.headers).status_code, 200)

    def test_saved_map_catalogue_is_authenticated_and_has_no_jobs_or_lifecycle_side_effects(self):
        for world in ('dev', 'test'):
            root = self.base / 'maps' / world
            root.mkdir(parents=True)
            (root / 'world.mt').write_text('gameid = minetest\n')
            self.config['worlds'][world] = str(root / 'civicrm_bridge')
        app = self.app()
        with TestClient(app) as client:
            self.assertEqual(client.get('/v1/worlds').status_code, 401)
            response = client.get('/v1/worlds', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.json(), {'version': 1, 'worlds': [
                {'world_id': 'dev', 'name': 'dev'}, {'world_id': 'test', 'name': 'test'}]})
            self.assertNotIn(str(self.base), response.text)
            self.assertEqual(app.state.jobs.rows('SELECT * FROM jobs'), [])
            (self.base / 'maps/test/world.mt').unlink()
            self.assertEqual(client.get('/v1/worlds', headers=self.headers).json()['worlds'],
                             [{'world_id': 'dev', 'name': 'dev'}])
        self.assertEqual(self.calls, [])

    def test_failed_catalogue_does_not_disclose_private_exception_text(self):
        with TestClient(self.app()) as client, patch('api.app.catalogue', side_effect=OSError('private path')):
            response = client.get('/v1/worlds', headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'version': 1, 'error': {'code': 'world_catalogue_unavailable'}})
            self.assertNotIn('private', response.text)

    def test_authentication_precedes_all_routing_and_side_effects(self):
        app = self.app()
        with TestClient(app) as client:
            for path in ('/v1/lifecycle/jobs', '/v1/worlds/dev/accounts/jobs', '/docs', '/unknown'):
                for auth in ({}, {'Authorization': 'Bearer invalid'}, {'Authorization': 'Basic anything'}):
                    response = client.post(path, content=b'not json', headers=auth)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['www-authenticate'], 'Bearer')
            response = client.post('/v1/lifecycle/jobs', content=lifecycle(), headers=[
                ('Authorization', 'Bearer ' + self.secret), ('Authorization', 'Bearer ' + self.secret),
                ('Content-Type', 'application/json')])
            self.assertEqual(response.status_code, 401)
            self.assertEqual(app.state.jobs.rows('SELECT * FROM jobs'), [])
            app.state.jobs.tick_accounts()
            app.state.jobs.tick_lifecycle()
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.base / 'dev/inbox').iterdir()), [])

    def test_rotation_and_revocation_without_restart(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/unknown', headers=self.headers).status_code, 404)
            tokens.generate(self.base / 'token.json', self.base / 'replacement')
            self.assertEqual(client.get('/unknown', headers=self.headers).status_code, 401)
            replacement = (self.base / 'replacement').read_text().strip()
            headers = {'Authorization': 'Bearer ' + replacement}
            self.assertEqual(client.get('/unknown', headers=headers).status_code, 404)
            tokens.revoke(self.base / 'token.json')
            self.assertEqual(client.get('/unknown', headers=headers).status_code, 401)
            self.assertFalse(tokens.active(self.base / 'token.json'))

    def test_private_token_delivery_and_unsafe_files(self):
        digest_raw = (self.base / 'token.json').read_bytes()
        self.assertNotIn(self.secret.encode(), digest_raw)
        self.assertEqual((self.base / 'delivery').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            tokens.generate(self.base / 'token.json', self.base / 'delivery')
        self.assertTrue(authenticated(self.base / 'token.json', [self.headers['Authorization']]))
        (self.base / 'token.json').chmod(0o666)
        self.assertFalse(authenticated(self.base / 'token.json', [self.headers['Authorization']]))
        (self.base / 'token.json').chmod(0o600)
        (self.base / 'token.json').rename(self.base / 'saved')
        (self.base / 'token.json').symlink_to(self.base / 'saved')
        self.assertFalse(authenticated(self.base / 'token.json', [self.headers['Authorization']]))

    def test_strict_validation_and_limits(self):
        bad = [b'{', b'[]', b'{"version":1,"version":1}', encoded({**account(), 'path': '/tmp'})]
        for alteration in ({'admitted': 'true'}, {'role': 'root'}, {'initial_password': ''},
                           {'initial_password': None}, {'username': '../ben'}, {'command': 'anything'}):
            value = account()
            value['accounts'][0].update(alteration)
            bad.append(encoded(value))
        bad.extend([encoded({**account(), 'version': True}), encoded({**account(), 'operation': 'delete'}),
                    encoded({**account(), 'accounts': []})])
        duplicate = account()
        duplicate['accounts'].append({**duplicate['accounts'][0], 'username': 'API_DEMO'})
        bad.append(encoded(duplicate))
        with TestClient(self.app()) as client:
            for raw in bad:
                self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=raw, headers=self.headers).status_code, 400)
            for raw in (lifecycle(operation='shell'), lifecycle(world='../world'), lifecycle(operation='start_world'),
                        lifecycle(operation='start_world', world='other')):
                self.assertEqual(client.post('/v1/lifecycle/jobs', content=raw, headers=self.headers).status_code, 400)
            for path, maximum in (('/v1/lifecycle/jobs', 16384), ('/v1/worlds/dev/accounts/jobs', 256 * 1024)):
                self.assertEqual(client.post(path, content=b' ' * (maximum + 1), headers=self.headers).status_code, 413)
            self.assertEqual(client.post('/v1/lifecycle/jobs', content=lifecycle(), headers={'Authorization': self.headers['Authorization']}).status_code, 415)
            self.assertEqual(client.post('/v1/lifecycle/jobs', content=lifecycle(), headers={**self.headers, 'Content-Encoding': 'gzip'}).status_code, 415)
            self.assertEqual(client.post('/v1/worlds/other/accounts/jobs', content=encoded(account()), headers=self.headers).status_code, 404)
            self.assertEqual(client.get('/docs', headers=self.headers).status_code, 404)
        self.assertEqual(self.calls, [])

    def test_account_exact_bytes_world_isolation_and_password_cleanup(self):
        app = self.app()
        raw = b'\n' + encoded(account()) + b'\n'
        with TestClient(app) as client:
            for world in ('dev', 'test'):
                response = client.post(f'/v1/worlds/{world}/accounts/jobs', content=raw, headers=self.headers)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.headers['location'], response.json()['result_url'])
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=raw, headers=self.headers).status_code, 202)
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=raw.strip(), headers=self.headers).status_code, 409)
            app.state.jobs.tick_accounts()
            self.assertEqual((self.base / 'dev/inbox/api-demo.json').read_bytes(), raw)
            self.assertEqual((self.base / 'test/inbox/api-demo.json').read_bytes(), raw)
            # A stale outbox is ignored while the job remains pending.
            self.result('dev')
            (self.base / 'dev/inbox/api-demo.json').write_bytes(raw)
            app.state.jobs.tick_accounts()
            self.assertEqual(client.get('/v1/worlds/dev/accounts/jobs/api-demo', headers=self.headers).json()['state'], 'pending')
            expected = self.result('dev', status='partial')
            app.state.jobs.tick_accounts()
            response = client.get('/v1/worlds/dev/accounts/jobs/api-demo', headers=self.headers)
            self.assertEqual(response.json()['result'], expected)
            self.assertNotIn('initial_password', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=raw, headers=self.headers).status_code, 200)
            row = app.state.jobs.rows("SELECT raw FROM jobs WHERE world='dev'")[0]
            self.assertIsNone(row['raw'])
            self.result('test')
            app.state.jobs.tick_accounts()
        # SQLite secure_delete clears the deleted password bytes from main pages.
        self.assertNotIn(b'FictitiousApiOnly!', (self.base / 'state/jobs.sqlite3').read_bytes())

    def test_lifecycle_fixed_arguments_replay_and_conflict(self):
        app = self.app()
        with TestClient(app) as client:
            for operation, world in (('status', None), ('stop_world', None), ('start_world', 'dev'), ('switch_world', 'test')):
                raw = lifecycle(operation, operation, world)
                self.assertEqual(client.post('/v1/lifecycle/jobs', content=raw, headers=self.headers).status_code, 202)
                app.state.jobs.tick_lifecycle()
                result = client.get('/v1/lifecycle/jobs/' + operation, headers=self.headers).json()
                self.assertEqual(result['state'], 'completed')
                self.assertEqual(client.post('/v1/lifecycle/jobs', content=raw, headers=self.headers).status_code, 200)
            self.assertEqual(self.calls, [list(world_worker.COMMAND) + [op] + ([world] if world else [])
                                        for op, world in (('status', None), ('stop_world', None), ('start_world', 'dev'), ('switch_world', 'test'))])
            self.assertEqual(client.post('/v1/lifecycle/jobs', content=lifecycle('status', 'stop_world'), headers=self.headers).status_code, 409)


class InstanceApiTests(Fixture):
    def setUp(self):
        super().setUp()
        from lifecycle.instances import runtime_for as resolve
        from lifecycle.templates import regular
        from tools.publish_template import publish
        from contextlib import closing
        import sqlite3
        self.identifier = 'g' + '0' * 26
        roots = self.base / 'instances'
        records = self.base / 'registry'
        records.mkdir()
        runtime = roots / self.identifier / 'civicrm_bridge'
        for name in ('inbox', 'outbox'):
            (runtime / name).mkdir(parents=True)
        self.record_path = records / (self.identifier + '.json')
        self.record = {'instance_id': self.identifier, 'archived': False, 'enabled': True,
                       'access_window': {'starts_at': 1, 'ends_at': 2**31}, 'generation': 1}
        self.record_path.write_bytes(encoded(self.record))
        source = self.base / 'clean'
        source.mkdir()
        (source / 'world.mt').write_text('gameid = minetest\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
        with closing(sqlite3.connect(source / 'map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
        self.manifest = publish(source, self.base / 'published', 'classroom', 'Classroom')
        self.config.update(instances_root=str(roots), instance_registry=str(records), templates={'classroom': str(self.base / 'published')})
        for target in ('api.app.runtime_for', 'api.jobs.runtime_for'):
            mocked = patch(target, side_effect=resolve)
            mocked.start()
            self.addCleanup(mocked.stop)
        def fixture_record(path):
            regular(path, {os.geteuid()})
            return json.loads(Path(path).read_text())
        reader = patch('lifecycle.instances.trusted_json', side_effect=fixture_record)
        reader.start()
        self.addCleanup(reader.stop)
        self.addCleanup(lambda: (self.base / 'published').chmod(0o700))

    def runner(self, arguments, raw=None):
        self.calls.append(arguments)
        return {'ok': True, 'state': 'stopped'}

    def test_template_catalogue_is_authenticated_and_templates_cannot_receive_accounts(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/v1/world-templates').status_code, 401)
            response = client.get('/v1/world-templates', headers=self.headers)
            self.assertEqual(response.json(), {'version': 1, 'templates': [
                {'template_id': 'classroom', 'name': 'Classroom', 'revision': self.manifest['revision']}]})
            for forbidden in ('classroom', 'dev', 'test'):
                self.assertEqual(client.post(f'/v1/worlds/{forbidden}/accounts/jobs', content=encoded(account()), headers=self.headers).status_code, 404)
        self.assertEqual(self.calls, [])

    def test_accounts_are_delivered_only_to_registered_copy_and_archival_blocks_new_jobs(self):
        with TestClient(self.app()) as client:
            raw = encoded(account())
            accepted = client.post(f'/v1/worlds/{self.identifier}/accounts/jobs', content=raw, headers=self.headers)
            self.assertEqual(accepted.status_code, 202)
            client.app.state.jobs.tick_accounts()
            runtime = Path(self.config['instances_root']) / self.identifier / 'civicrm_bridge'
            self.assertEqual((runtime / 'inbox/api-demo.json').read_bytes(), raw)
            self.record['archived'] = True
            self.record_path.write_bytes(encoded(self.record))
            self.assertEqual(client.post(f'/v1/worlds/{self.identifier}/accounts/jobs', content=encoded(account('new')), headers=self.headers).status_code, 404)
            self.assertEqual(client.get(accepted.json()['result_url'], headers=self.headers).status_code, 200)

    def test_v2_lifecycle_contract_uses_restricted_stdin_operation_and_exact_replay(self):
        value = {'version': 2, 'job_id': 'instance-create', 'operation': 'create_instance', 'instance_id': self.identifier,
                 'template_id': 'classroom', 'template_revision': self.manifest['revision'], 'access_window': {'starts_at': 1, 'ends_at': 2}}
        with TestClient(self.app()) as client:
            response = client.post('/v1/lifecycle/jobs', content=encoded(value), headers=self.headers)
            self.assertEqual(response.status_code, 202)
            client.app.state.jobs.tick_lifecycle()
            completed = client.get(response.json()['result_url'], headers=self.headers).json()
            self.assertEqual(completed['result']['version'], 2)
            self.assertEqual(completed['result']['instance_id'], self.identifier)
            self.assertIn([*world_worker.COMMAND, 'instance'], self.calls)
            self.assertEqual(client.post('/v1/lifecycle/jobs', content=encoded(value), headers=self.headers).status_code, 200)


class RecoveryTests(Fixture):
    def jobs(self):
        return Jobs(self.config, self.runner)

    def test_concurrent_acceptance_and_single_service_lock(self):
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: jobs.accept('accounts', 'dev', 'api-demo', encoded(account())), range(20)))
        self.assertEqual(len(jobs.rows('SELECT * FROM jobs')), 1)
        with self.assertRaises(BlockingIOError):
            self.jobs()
        with self.assertRaises(Conflict):
            jobs.accept('accounts', 'dev', 'api-demo', encoded(account(admitted=False)))

    def test_queued_work_resumes_without_repeating_dispatched_lifecycle(self):
        jobs = self.jobs()
        jobs.accept('accounts', 'dev', 'api-demo', encoded(account()))
        jobs.accept('lifecycle', '', 'life-demo', lifecycle())
        jobs.close()
        jobs = self.jobs()
        jobs.tick_accounts()
        self.assertTrue((self.base / 'dev/inbox/api-demo.json').exists())
        row = jobs.rows("SELECT * FROM jobs WHERE kind='lifecycle'")[0]
        jobs.update(row, 'dispatching')
        jobs.close()
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        jobs.tick_lifecycle()
        self.assertEqual(jobs.get('lifecycle', '', 'life-demo')['state'], 'outcome_unknown')
        self.assertEqual(self.calls, [])

    def test_completed_worker_receipt_recovers_without_controller_call(self):
        jobs = self.jobs()
        jobs.accept('lifecycle', '', 'life-demo', lifecycle())
        row = jobs.rows('SELECT * FROM jobs')[0]
        jobs.update(row, 'dispatching')
        expected = world_worker.handle(lifecycle(), self.base / 'state/lifecycle', runner=self.runner)
        jobs.close()
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        self.assertEqual(jobs.get('lifecycle', '', 'life-demo')['result'], expected)
        self.assertEqual(len(self.calls), 1)

    def test_account_publish_crash_and_stale_result_recovery(self):
        jobs = self.jobs()
        jobs.accept('accounts', 'dev', 'api-demo', encoded(account()))
        row = jobs.rows('SELECT * FROM jobs')[0]
        old = self.result('dev')
        baseline = hashlib.sha256(encoded(old)).hexdigest()
        jobs.update(row, 'dispatching', baseline=baseline)
        jobs.close()
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        self.assertEqual(jobs.get('accounts', 'dev', 'api-demo')['state'], 'outcome_unknown')
        jobs.tick_accounts()
        self.assertFalse((self.base / 'dev/inbox/api-demo.json').exists())

    def test_account_pending_file_recovers_then_completes(self):
        jobs = self.jobs()
        jobs.accept('accounts', 'dev', 'api-demo', encoded(account()))
        row = jobs.rows('SELECT * FROM jobs')[0]
        jobs.update(row, 'dispatching')
        (self.base / 'dev/inbox/api-demo.json').write_bytes(encoded(account()))
        jobs.close()
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        self.assertEqual(jobs.get('accounts', 'dev', 'api-demo')['state'], 'pending')
        self.result('dev')
        jobs.tick_accounts()
        self.assertEqual(jobs.get('accounts', 'dev', 'api-demo')['state'], 'completed')
        self.assertNotIn(b'FictitiousApiOnly!', (self.base / 'state/jobs.sqlite3').read_bytes())

    def test_pending_job_is_never_overwritten_and_symlink_is_not_followed(self):
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        jobs.accept('accounts', 'dev', 'api-demo', encoded(account()))
        other = encoded(account(admitted=False))
        pending = self.base / 'dev/inbox/api-demo.json'
        pending.write_bytes(other)
        jobs.tick_accounts()
        self.assertEqual(pending.read_bytes(), other)
        self.assertEqual(jobs.get('accounts', 'dev', 'api-demo')['state'], 'outcome_unknown')
        jobs.accept('accounts', 'test', 'api-demo', encoded(account()))
        (self.base / 'test/inbox/api-demo.json').symlink_to(pending)
        jobs.tick_accounts()
        self.assertEqual(pending.read_bytes(), other)
        self.assertEqual(jobs.get('accounts', 'test', 'api-demo')['state'], 'outcome_unknown')

    def test_corrupt_result_does_not_leave_consumed_job_pending_forever(self):
        jobs = self.jobs()
        self.addCleanup(jobs.close)
        jobs.accept('accounts', 'dev', 'api-demo', encoded(account()))
        jobs.tick_accounts()
        self.result('dev')
        (self.base / 'dev/outbox/api-demo.json').write_bytes(b'not JSON')
        jobs.tick_accounts()
        self.assertEqual(jobs.get('accounts', 'dev', 'api-demo')['state'], 'outcome_unknown')


class InstallerTests(Fixture):
    def test_hostname_validation_and_service_boundary(self):
        for invalid in ('../bad', 'https://bridge.example.org', '127.0.0.1', 'host.local', 'bad;example.org', 'single'):
            with self.assertRaises(ValueError):
                hostname(invalid)
        self.assertEqual(hostname('Bridge.Example.org'), 'bridge.example.org')
        service = unit(Path('/usr/local/lib/civicrm-bridge-api/venv-example'))
        self.assertIn(b'User=civicrm_bridge_api', service)
        self.assertIn(b'/var/lib/civicrm-worldctl /run/civicrm-worldctl.lock', service)
        self.assertNotIn(b'NoNewPrivileges=true\n', service)
        self.assertNotIn(b'systemctl restart luanti', service)
        files = deployment_files('bridge.example.org', os.geteuid())
        self.assertEqual(set(files), {'config.json', 'civicrm-bridge-api.service',
                                     'civicrm-bridge-api.sudoers', 'civicrm-bridge-api.tmpfiles',
                                     'civicrm-bridge-token', 'token_cli.py'})
        self.assertEqual(json.loads(files['config.json'])['world_owner_uid'], os.geteuid())
        self.assertNotIn(b'*', files['civicrm-bridge-api.sudoers'])
        self.assertEqual(files['civicrm-bridge-api.tmpfiles'], b'f /run/civicrm-worldctl.lock 0600 root root -\n')

    def test_deployment_preparer_never_runs_system_commands(self):
        from api.install import main
        output = self.base / 'prepared'
        with patch('sys.argv', ['install.py', '--hostname', 'bridge.example.org', '--output', str(output)]), \
                patch('os.geteuid', return_value=1000), patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1000)), \
                patch('subprocess.run') as run:
            main()
        run.assert_not_called()
        self.assertEqual(len(list(output.iterdir())), 6)
        with patch('sys.argv', ['install.py', '--hostname', 'bridge.example.org', '--output', str(output)]), \
                patch('os.geteuid', return_value=1000), patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1000)):
            with self.assertRaises(FileExistsError):
                main()


@unittest.skipUnless(shutil.which('luanti'), 'Luanti is required for disposable engine transport tests')
class EngineTests(Fixture):
    def test_v2_api_time_boundaries_late_job_and_restart(self):
        from run_integration import Harness
        harness = Harness(shutil.which('luanti'), ROOT, self.base,
                          Path(os.environ.get('BRIDGE_TEST_GAME', '/home/ben/.minetest/games/minetest_game')))
        self.addCleanup(shutil.rmtree, harness.path)
        self.addCleanup(harness.stop)
        harness.start()
        self.config['worlds']['dev'] = str(harness.runtime)
        start, end = 1800000000, 1800000060
        value = account('api-window')
        value['version'] = 2
        value['accounts'][0]['access_window'] = {'starts_at': start, 'ends_at': end}
        harness.control_command('set_time', timestamp=start - 1)
        with TestClient(self.app(background=True)) as client:
            response = client.post('/v1/worlds/dev/accounts/jobs', content=encoded(value), headers=self.headers)
            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = client.get(response.json()['result_url'], headers=self.headers).json()
                if result['state'] == 'completed':
                    break
                time.sleep(.05)
            self.assertEqual(result['result']['version'], 2, result)
            self.assertEqual(result['result']['accounts'][0]['status'], 'created')
            self.assertTrue(harness.snapshot('api_demo')['denial'])
            harness.control_command('set_time', timestamp=start)
            self.assertFalse(harness.snapshot('api_demo')['denial'])
            harness.control_command('mock_online', username='api_demo')
            harness.control_command('set_time', timestamp=end)
            deadline = time.monotonic() + 2
            while not harness.snapshot('api_demo')['kicked'] and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(harness.snapshot('api_demo')['kicked'])
            harness.stop()
            harness.start()
            harness.control_command('set_time', timestamp=end)
            self.assertTrue(harness.snapshot('api_demo')['denial'])
            self.assertEqual(client.post('/v1/worlds/dev/accounts/jobs', content=encoded(value),
                                         headers=self.headers).status_code, 200)
            self.assertTrue(harness.snapshot('api_demo')['denial'])
            harness.stop()
            late = json.loads(encoded(value))
            late['job_id'] = 'api-late-window'
            late['accounts'][0]['username'] = 'late_api'
            waiting = client.post('/v1/worlds/dev/accounts/jobs', content=encoded(late), headers=self.headers)
            self.assertEqual(waiting.status_code, 202)
            self.assertEqual(waiting.json()['state'], 'pending')
            harness.start()
            harness.control_command('set_time', timestamp=end)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = client.get(waiting.json()['result_url'], headers=self.headers).json()
                if result['state'] == 'completed':
                    break
                time.sleep(.05)
            self.assertEqual(result['result']['accounts'][0]['status'], 'created')
            self.assertTrue(harness.snapshot('late_api')['denial'])

    def test_api_import_preserves_password_and_enforces_withdrawal(self):
        from run_integration import Harness
        harness = Harness(shutil.which('luanti'), ROOT, self.base,
                          Path(os.environ.get('BRIDGE_TEST_GAME', '/home/ben/.minetest/games/minetest_game')))
        self.addCleanup(shutil.rmtree, harness.path)
        self.addCleanup(harness.stop)
        harness.start()
        self.config['worlds']['dev'] = str(harness.runtime)
        with TestClient(self.app(background=True)) as client:
            def submit(value):
                response = client.post('/v1/worlds/dev/accounts/jobs', content=encoded(value), headers=self.headers)
                self.assertEqual(response.status_code, 202)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = client.get(response.json()['result_url'], headers=self.headers).json()
                    if result['state'] == 'completed':
                        return result['result']
                    time.sleep(.05)
                self.fail('Disposable engine account job did not complete: ' + str(result))
            result = submit(account())
            self.assertEqual(result['accounts'][0]['status'], 'created')
            harness.control_command('set_password', username='api_demo', password='FictitiousPlayerChanged!')
            password = harness.snapshot('api_demo')['password']
            harness.control_command('mock_online', username='api_demo')
            withdrawal = account('api-withdraw', admitted=False)
            withdrawal['accounts'][0]['role'] = 'facilitator'
            result = submit(withdrawal)
            self.assertEqual(result['accounts'][0]['status'], 'updated')
            snapshot = harness.snapshot('api_demo')
            self.assertTrue(snapshot['password'] == password)
            self.assertTrue(snapshot['privileges'].get('fly'))
            self.assertTrue(snapshot['denial'])
            self.assertTrue(snapshot['kicked'])
            # Replaying the old creation decision through the API never readmits.
            replay = client.post('/v1/worlds/dev/accounts/jobs', content=encoded(account()), headers=self.headers)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(harness.snapshot('api_demo')['denial'])


@unittest.skipUnless(os.environ.get('CADDY_BIN'), 'Set CADDY_BIN for disposable real HTTPS/proxy tests')
class HttpsTests(Fixture):
    def test_caddy_https_authenticated_jobs(self):
        def free_port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]
        api_port, https_port, http_port = free_port(), free_port(), free_port()
        cert, key = self.base / 'cert.pem', self.base / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
                        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        server = uvicorn.Server(uvicorn.Config(self.app(background=True), host='127.0.0.1', port=api_port,
                                              access_log=False, log_level='critical'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        def close_server():
            server.should_exit = True
            thread.join(timeout=5)
        self.addCleanup(close_server)
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(server.started)
        caddy = Path(os.environ['CADDY_BIN'])
        config = self.base / 'Caddyfile'
        config.write_text('{\n admin off\n auto_https off\n}\n' +
                          f'https://localhost:{https_port} {{\n bind 127.0.0.1\n tls {cert} {key}\n'
                          f' reverse_proxy 127.0.0.1:{api_port}\n}}\n' +
                          f'http://localhost:{http_port} {{\n bind 127.0.0.1\n respond "HTTPS required" 426\n}}\n')
        environment = {**os.environ, 'HOME': str(self.base), 'XDG_DATA_HOME': str(self.base / 'caddy-data'),
                       'XDG_CONFIG_HOME': str(self.base / 'caddy-config')}
        log = open(self.base / 'caddy.log', 'wb')
        self.addCleanup(log.close)
        process = subprocess.Popen([str(caddy), 'run', '--config', str(config), '--adapter', 'caddyfile'],
                                   env=environment, stdout=log, stderr=log)
        def close_caddy():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.addCleanup(close_caddy)
        context = ssl.create_default_context(cafile=str(cert))
        with httpx.Client(verify=context, trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = client.get(f'https://localhost:{https_port}/unknown')
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        self.fail('Disposable Caddy HTTPS listener did not become ready')
                    time.sleep(.05)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(client.get(f'http://localhost:{http_port}/v1/lifecycle/jobs').status_code, 426)
            url = f'https://localhost:{https_port}'
            submitted = client.post(url + '/v1/lifecycle/jobs', content=lifecycle(), headers=self.headers)
            self.assertEqual(submitted.status_code, 202)
            result_url = submitted.json()['result_url']
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = client.get(url + result_url, headers=self.headers).json()
                if result['state'] == 'completed':
                    break
                time.sleep(.05)
            self.assertEqual(result['result']['ok'], True)
            self.assertEqual(len(self.calls), 1)
            tokens.revoke(self.base / 'token.json')
            self.assertEqual(client.post(url + '/v1/lifecycle/jobs', content=lifecycle('denied'), headers=self.headers).status_code, 401)
        self.assertNotIn(self.secret.encode(), (self.base / 'caddy.log').read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
