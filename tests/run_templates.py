#!/usr/bin/env python3
"""Disposable tests for clean immutable template publication; no running services."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifecycle.templates import catalogue, inspect_bundle, inspect_content
from lifecycle.instance_contract import validate
from lifecycle.controller import LifecycleError
from tools.publish_template import publish


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'world.mt').write_text('gameid = minetest\nbackend = sqlite3\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
        with closing(sqlite3.connect(self.source / 'map.sqlite')) as db:
            db.execute('CREATE TABLE blocks (pos INTEGER PRIMARY KEY, data BLOB)')
            db.execute("INSERT INTO blocks VALUES (1, 'template block')")
            db.commit()

    def tearDown(self):
        for root, _, files in os.walk(self.base):
            Path(root).chmod(0o700)
            for name in files:
                if not (Path(root) / name).is_symlink():
                    (Path(root) / name).chmod(0o600)
        self.temporary.cleanup()

    def test_publish_preserves_map_and_produces_stable_revision(self):
        first = publish(self.source, self.base / 'first', 'classroom', 'Classroom')
        second = publish(self.source, self.base / 'second', 'classroom', 'Classroom')
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual(inspect_bundle(self.base / 'first'), first)
        self.assertEqual((self.base / 'first/map.sqlite').read_bytes(), (self.source / 'map.sqlite').read_bytes())
        with self.assertRaises(ValueError):
            publish(self.source, self.base / 'first', 'classroom', 'Classroom')

    def test_account_player_runtime_and_executable_files_are_rejected(self):
        for filename in ('auth.sqlite', 'players.sqlite', 'civicrm_bridge', 'worldmods', 'debug.txt', 'map.sqlite-wal'):
            with self.subTest(filename=filename):
                extra = self.source / filename
                extra.write_text('private')
                with self.assertRaises(ValueError):
                    inspect_content(self.source)
                extra.unlink()

    def test_bridge_mod_storage_cannot_leak_into_a_copy(self):
        with closing(sqlite3.connect(self.source / 'mod_storage.sqlite')) as db:
            db.execute('CREATE TABLE entries (modname TEXT, key TEXT, value BLOB)')
            db.execute("INSERT INTO entries VALUES ('civicrm_bridge', 'state', 'private admission')")
            db.commit()
        with self.assertRaises(ValueError):
            inspect_content(self.source)

    def test_changed_published_content_is_rejected(self):
        destination = self.base / 'published'
        publish(self.source, destination, 'classroom', 'Classroom')
        destination.chmod(0o700)
        (destination / 'world.mt').chmod(0o600)
        (destination / 'world.mt').write_text('gameid = another_game\n')
        with self.assertRaises(ValueError):
            inspect_bundle(destination)

    def test_symlinks_and_non_sqlite_backends_are_rejected(self):
        (self.source / 'world.mt').write_text('gameid = minetest\nbackend = postgresql\n')
        with self.assertRaises(ValueError):
            inspect_content(self.source)
        (self.source / 'world.mt').unlink()
        try:
            (self.source / 'world.mt').symlink_to(self.base / 'outside')
        except OSError:
            return
        with self.assertRaises(ValueError):
            inspect_content(self.source)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inherited ACL fixture')
    def test_publication_clears_inherited_acls_that_would_deny_api_reading(self):
        import pwd
        identity = pwd.getpwnam('nobody').pw_uid
        parent = self.base / 'restricted-parent'
        parent.mkdir()
        subprocess.run(['setfacl', '-m', f'd:u:{identity}:--x', str(parent)], check=True)
        destination = parent / 'published'
        publish(self.source, destination, 'classroom', 'Classroom')
        for path in [destination, *destination.iterdir()]:
            attributes = os.listxattr(path)
            self.assertNotIn('system.posix_acl_access', attributes)
            self.assertNotIn('system.posix_acl_default', attributes)
        inspect_bundle(destination)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux traverse-only ancestor fixture')
    def test_catalogue_can_traverse_private_ancestors_without_listing_them(self):
        parent = self.base / 'private-home'
        parent.mkdir()
        destination = parent / 'published'
        publish(self.source, destination, 'classroom', 'Classroom')
        parent.chmod(0o111)
        try:
            result = catalogue({'templates': {'classroom': str(destination)}, 'world_owner_uid': os.getuid()})
            self.assertEqual(result['templates'][0]['template_id'], 'classroom')
        finally:
            parent.chmod(0o700)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux metadata-only ACL checks')
    def test_metadata_only_template_checks_still_reject_unsafe_ancestor_writes(self):
        import pwd
        parent = self.base / 'unsafe-parent'
        parent.mkdir()
        destination = parent / 'published'
        publish(self.source, destination, 'classroom', 'Classroom')
        config = {'templates': {'classroom': str(destination)}, 'world_owner_uid': os.getuid()}
        parent.chmod(0o733)
        with self.assertRaises(LifecycleError):
            catalogue(config)
        parent.chmod(0o700)
        identity = pwd.getpwnam('nobody').pw_uid
        subprocess.run(['setfacl', '-m', f'u:{identity}:rwx', str(parent)], check=True)
        with self.assertRaises(LifecycleError):
            catalogue(config)
        subprocess.run(['setfacl', '-b', str(parent)], check=True)
        self.assertEqual(catalogue(config)['templates'][0]['template_id'], 'classroom')

    def test_instance_contract_rejects_paths_unknown_fields_and_invalid_windows(self):
        valid = {'version': 2, 'job_id': 'create-1', 'operation': 'create_instance', 'instance_id': 'g' + '0' * 26,
                 'template_id': 'classroom', 'template_revision': 'a' * 64, 'access_window': {'starts_at': 1, 'ends_at': 2}}
        self.assertEqual(validate(valid), valid)
        for changed in ({'instance_id': '../escape'}, {'template_id': '/tmp/map'}, {'path': '/tmp/map'},
                        {'access_window': {'starts_at': True, 'ends_at': 2}}, {'access_window': {'starts_at': 2, 'ends_at': 2}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate({**valid, **changed})

if __name__ == '__main__':
    unittest.main(verbosity=2)
