#!/usr/bin/env python3
"""Configuration-only upgrade tests; no services or installed files are changed."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.upgrade import CONTROLLER, PUBLISHER, TEMPLATES, encoded, registration
from lifecycle.install import configuration_for_upgrade
import api.upgrade as upgrade
from lifecycle.templates import catalogue
from tools.publish_template import publish


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.controller = {'templates': {'other': '/retained/template'},
                           'template_revisions': {'other': ['/retained/old']},
                           'instances_root': '/instances', 'instance_registry': '/registry',
                           'worlds': {'dev': '/old/dev', 'test': '/old/test'}}
        self.api = {'token_file': '/private/existing-token.json', 'state_dir': '/private/jobs',
                    'hostname': 'bridge.example.org', 'world_owner_uid': 1000, 'worlds': {'dev': '/old/runtime'}}
        self.raw = json.dumps(self.controller).encode()
        self.manifest = {'version': 1, 'sha256': {str(CONTROLLER): hashlib.sha256(self.raw).hexdigest(),
                                               '/existing/controller.py': 'retained'}}

    def upgrade(self, controller=None, api=None, manifest=None):
        return registration(self.raw if controller is None else controller,
                            encoded(self.api if api is None else api),
                            encoded(self.manifest if manifest is None else manifest), b'reviewed publisher')

    def test_registration_retains_existing_settings_and_updates_managed_hashes(self):
        before = encoded(self.api)
        controller_raw, api_raw, manifest_raw = self.upgrade()
        controller, api, manifest = map(json.loads, (controller_raw, api_raw, manifest_raw))
        self.assertEqual(encoded(self.api), before)
        for key, value in self.api.items():
            self.assertEqual(api[key], value)
        self.assertEqual(api['templates'], TEMPLATES)
        self.assertEqual(controller['templates'], {'other': '/retained/template', **TEMPLATES})
        self.assertEqual(controller['template_revisions'], self.controller['template_revisions'])
        self.assertEqual(api['instances_root'], '/instances')
        self.assertEqual(api['instance_registry'], '/registry')
        self.assertEqual(manifest['sha256'][str(CONTROLLER)], hashlib.sha256(controller_raw).hexdigest())
        self.assertEqual(manifest['sha256'][str(PUBLISHER)], hashlib.sha256(b'reviewed publisher').hexdigest())
        self.assertEqual(manifest['sha256']['/existing/controller.py'], 'retained')

    def test_registration_can_be_repeated_without_changing_the_result(self):
        first = self.upgrade()
        self.assertEqual(registration(*first, b'reviewed publisher'), first)

    def test_unrecognized_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.upgrade(controller=self.raw + b' ')

    def test_conflicting_template_or_instance_paths_are_rejected(self):
        for api in ({**self.api, 'templates': {'dev': '/different/template'}},
                    {**self.api, 'instances_root': '/different/instances'},
                    {**self.api, 'instance_registry': '/different/registry'}):
            with self.subTest(api=api), self.assertRaises(ValueError):
                self.upgrade(api=api)

    def test_later_controller_upgrades_retain_registered_revisions(self):
        configuration = configuration_for_upgrade(self.controller)
        self.assertEqual(configuration['templates'], self.controller['templates'])
        self.assertEqual(configuration['template_revisions'], self.controller['template_revisions'])
        self.assertEqual(configuration['instances_root'], '/home/ben/luanti-dev/instances')


class ActivationFlowTests(unittest.TestCase):
    """Exercise writes/rollback in a private tree with all privileged commands replaced."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.paths = {'LIB': self.base / 'lib', 'CONTROLLER': self.base / 'controller/config.json',
                      'CONFIG': self.base / 'api/config.json', 'MANIFEST': self.base / 'state/install-manifest.json',
                      'PUBLISHER': self.base / 'controller/publish_template.py',
                      'DROPIN': self.base / 'systemd/api.service.d', 'SUDOERS': self.base / 'sudoers/private-games'}
        self.controller = {'templates': {}, 'instances_root': '/home/ben/luanti-dev/instances',
                           'instance_registry': '/var/lib/civicrm-worldctl/instances'}
        self.api = {'templates': {}, 'world_owner_uid': os.getuid(),
                    'token_file': '/etc/civicrm-bridge-api/token.json', 'state_dir': '/var/lib/civicrm-bridge-api',
                    'hostname': 'bridge.example.org'}
        for name, path in self.paths.items():
            if name != 'LIB':
                path.parent.mkdir(parents=True, exist_ok=True)
        self.paths['LIB'].mkdir()
        self.paths['CONTROLLER'].write_bytes(encoded(self.controller))
        self.paths['CONFIG'].write_bytes(encoded(self.api))
        self.paths['PUBLISHER'].write_bytes(b'old installed publisher')
        self.paths['MANIFEST'].write_bytes(encoded({'version': 1, 'sha256': {
            str(self.paths['CONTROLLER']): hashlib.sha256(self.paths['CONTROLLER'].read_bytes()).hexdigest(),
            str(self.paths['PUBLISHER']): hashlib.sha256(self.paths['PUBLISHER'].read_bytes()).hexdigest()}}))
        api_dir = self.paths['LIB'] / 'api'
        api_dir.mkdir()
        (api_dir / 'requirements.txt').write_bytes((upgrade.ROOT / 'api/requirements.txt').read_bytes())
        (api_dir / 'app.py').write_bytes(b'old installed api')
        self.lock = self.base / 'lock'
        self.lock.touch()
        source = self.base / 'source'
        source.mkdir()
        (source / 'world.mt').write_text('gameid = minetest\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
        with closing(sqlite3.connect(source / 'map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
            db.commit()
        self.templates = {}
        for identifier in ('dev', 'test'):
            destination = self.base / (identifier + '-v1')
            publish(source, destination, identifier, identifier)
            self.templates[identifier] = str(destination)
        self.originals = {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        self.calls = []
        self.old_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask)
        for path in self.base.rglob('*'):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        self.temporary.cleanup()

    def activate(self, fail_verification=False):
        def write(path, raw, mode, **kwargs):
            path.write_bytes(raw)
            path.chmod(mode)

        def command(*args):
            self.calls.append(args)
            if args[0] in ('/usr/bin/getfacl', '/usr/bin/setfacl'):
                return subprocess.check_output(args)
            if args[0] == '/usr/bin/systemctl' and args[1] == 'show':
                return b'active\n'
            if args[0] == '/usr/bin/systemd-analyze' and fail_verification:
                raise RuntimeError('fixture verification failure')
            if args[0] == '/usr/sbin/runuser':
                config = json.loads(self.paths['CONFIG'].read_bytes())
                self.assertEqual({item['template_id'] for item in catalogue(config)['templates']}, {'dev', 'test'})
                self.assertEqual((self.paths['LIB'] / 'api/app.py').read_bytes(), (upgrade.ROOT / 'api/app.py').read_bytes())
            return b''

        with ExitStack() as stack:
            for name, value in self.paths.items():
                stack.enter_context(patch.object(upgrade, name, value))
            stack.enter_context(patch.object(upgrade, 'TEMPLATES', self.templates))
            stack.enter_context(patch.object(upgrade, 'trusted', lambda path: path.read_bytes()))
            stack.enter_context(patch.object(upgrade, 'directory', lambda path: path.mkdir(parents=True, exist_ok=True)))
            stack.enter_context(patch.object(upgrade, 'check_parents', lambda path: None))
            stack.enter_context(patch.object(upgrade, 'atomic_private', write))
            stack.enter_context(patch.object(upgrade, 'controller_lock', lambda: self.lock.open('r+')))
            stack.enter_context(patch.object(upgrade, 'command', command))
            stack.enter_context(patch.object(upgrade.os, 'geteuid', lambda: 0))
            stack.enter_context(patch.object(upgrade.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.getuid())))
            stack.enter_context(redirect_stdout(io.StringIO()))
            upgrade.main()

    def test_full_activation_updates_both_configurations_and_only_restarts_api(self):
        self.activate()
        for key in ('CONTROLLER', 'CONFIG'):
            config = json.loads(self.paths[key].read_bytes())
            self.assertEqual(config['templates'], self.templates)
        api = json.loads(self.paths['CONFIG'].read_bytes())
        self.assertEqual(api['token_file'], self.api['token_file'])
        self.assertEqual(api['state_dir'], self.api['state_dir'])
        manifest = json.loads(self.paths['MANIFEST'].read_bytes())
        for key in ('CONTROLLER', 'PUBLISHER'):
            path = self.paths[key]
            self.assertEqual(manifest['sha256'][str(path)], hashlib.sha256(path.read_bytes()).hexdigest())
        systemctl = [args for args in self.calls if args[0] == '/usr/bin/systemctl']
        self.assertIn(('/usr/bin/systemctl', 'stop', upgrade.SERVICE), systemctl)
        self.assertIn(('/usr/bin/systemctl', 'start', upgrade.SERVICE), systemctl)
        self.assertFalse(any('luanti-dev.service' in args for args in self.calls))

    def test_verification_failure_restores_previous_files_and_service(self):
        with self.assertRaises(RuntimeError):
            self.activate(fail_verification=True)
        for path, raw in self.originals.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        self.assertFalse((self.paths['LIB'] / 'lifecycle/instances.py').exists())
        self.assertFalse((self.paths['DROPIN'] / 'private-games.conf').exists())
        self.assertFalse(self.paths['SUDOERS'].exists())
        self.assertIn(('/usr/bin/systemctl', 'start', upgrade.SERVICE), self.calls)

    def test_code_directory_is_readable_despite_restrictive_install_umask(self):
        path = self.base / 'new-code-directory'
        original_lstat = Path.lstat

        def root_owned_metadata(value):
            fields = list(original_lstat(value))
            fields[4] = 0
            return os.stat_result(fields)

        with patch.object(upgrade, 'check_parents'), patch.object(Path, 'lstat', root_owned_metadata):
            upgrade.directory(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            path.chmod(0o700)
            upgrade.directory(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_failed_command_names_the_step_without_printing_stderr(self):
        result = subprocess.CompletedProcess([], 1, b'', b'private diagnostic fixture')
        with patch.object(upgrade.subprocess, 'run', return_value=result):
            with self.assertRaises(upgrade.CommandFailed) as caught:
                upgrade.command('/usr/sbin/runuser', '-u', 'civicrm_bridge_api', '--', 'python3')
        self.assertIn('API-user catalogue/import verification', str(caught.exception))
        self.assertNotIn('private diagnostic fixture', str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
