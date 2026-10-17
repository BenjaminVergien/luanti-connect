#!/usr/bin/env python3
"""Linux transactional installer tests; only disposable root-owned directories."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lifecycle.errors import LifecycleError
from lifecycle import install_docker


@unittest.skipUnless(os.name == 'posix', 'Linux ownership and atomic publication required')
class InstallerTransactions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / 'controller.py'
        self.original.write_bytes(b'previous reviewed source')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_bytes(b'{"version":1}')
        self.manifest.chmod(0o600)
        self.state = self.root / 'private-state'
        self.state.mkdir(mode=0o700)
        self.patch = patch.object(install_docker, 'STATE', self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_failed_upgrade_restores_source_modes_and_manifest_and_retains_runtime(self):
        runtime = self.root / 'retained-save'
        runtime.write_bytes(b'save progress')
        new = self.root / 'new-module.py'
        with patch.object(install_docker, 'command'), self.assertRaises(RuntimeError):
            with install_docker.transaction({self.original: (b'new source', 0o644), new: (b'new module', 0o644)}, self.manifest, {'version': 2}):
                self.assertEqual(b'new source', self.original.read_bytes())
                raise RuntimeError('Simulated activation failure')
        self.assertEqual(b'previous reviewed source', self.original.read_bytes())
        self.assertEqual(b'{"version":1}', self.manifest.read_bytes())
        self.assertFalse(new.exists())
        self.assertEqual(b'save progress', runtime.read_bytes())
        self.assertEqual(1, len(list(self.state.glob('docker-backup-*'))))

    def test_successful_upgrade_publishes_manifest_only_after_validation(self):
        with patch.object(install_docker, 'command'):
            with install_docker.transaction({self.original: (b'new source', 0o644)}, self.manifest, {'version': 2}):
                self.assertEqual(b'{"version":1}', self.manifest.read_bytes())
        self.assertEqual(2, json.loads(self.manifest.read_bytes())['version'])

    def test_large_reviewed_assets_can_be_updated_without_relaxing_default_secret_file_limits(self):
        self.original.write_bytes(b'a' * (1024 * 1024 + 1))
        with patch.object(install_docker, 'command'):
            with install_docker.transaction({self.original: (b'b' * (1024 * 1024 + 1), 0o644)}, self.manifest, {'version': 2}):
                pass
        self.assertEqual(b'b', self.original.read_bytes()[:1])

    def test_unrecognized_symlink_is_never_followed_or_overwritten(self):
        link = self.root / 'linked.py'
        link.symlink_to(self.original)
        with patch.object(install_docker, 'command'), self.assertRaises(OSError):
            with install_docker.transaction({link: (b'untrusted replacement', 0o644)}, self.manifest, {'version': 2}):
                pass
        self.assertEqual(b'previous reviewed source', self.original.read_bytes())

    def test_upgrade_removal_recovers_an_ambiguous_result_by_inspection_without_deleting_volumes(self):
        backend = Mock()
        identifier = 'a' * 64
        backend.inspect_owned.side_effect = [({'Id': identifier, 'State': {'Running': False}}, {}), (None, {})]
        backend.run.side_effect = LifecycleError('outcome_unknown', 'Interrupted removal')
        install_docker.retire_stopped(backend)
        backend.run.assert_called_once_with(['container', 'rm', identifier], mutation=True)

    def test_upgrade_never_removes_an_active_or_unrecognized_container(self):
        for problem in ('active', 'unrecognized'):
            with self.subTest(problem=problem):
                backend = Mock()
                if problem == 'active':
                    backend.inspect_owned.return_value = ({'Id': 'a' * 64, 'State': {'Running': True}}, {})
                else:
                    backend.inspect_owned.side_effect = LifecycleError('unmanaged_instance', 'Unrecognized container')
                with self.assertRaises((ValueError, LifecycleError)):
                    install_docker.retire_stopped(backend)
                backend.run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
