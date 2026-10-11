#!/usr/bin/env python3
"""Portable, disposable tests for saved-map discovery; never starts Luanti."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.worlds import catalogue


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='map-catalogue-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = {'worlds': {}}

    def world(self, world_id):
        root = self.base / world_id
        root.mkdir()
        (root / 'world.mt').write_text('gameid = minetest\n', encoding='utf-8')
        self.config['worlds'][world_id] = str(root / 'civicrm_bridge')
        return root

    def test_saved_maps_are_listed_deterministically_without_processes_or_runtime_directories(self):
        self.world('test')
        self.world('dev')
        with patch('subprocess.run') as run:
            result = catalogue(self.config)
        self.assertEqual(result, {'version': 1, 'worlds': [
            {'world_id': 'dev', 'name': 'dev'}, {'world_id': 'test', 'name': 'test'}]})
        self.assertNotIn(str(self.base), json.dumps(result))
        run.assert_not_called()

    def test_unconfigured_maps_are_not_exposed(self):
        self.world('dev')
        unconfigured = self.base / 'private'
        unconfigured.mkdir()
        (unconfigured / 'world.mt').write_text('gameid = minetest\n')
        self.assertEqual([item['world_id'] for item in catalogue(self.config)['worlds']], ['dev'])

    def test_removed_map_is_omitted_and_empty_catalogue_is_valid(self):
        root = self.world('dev')
        (root / 'world.mt').unlink()
        self.assertEqual(catalogue(self.config), {'version': 1, 'worlds': []})

    def test_settings_must_be_a_regular_file(self):
        root = self.world('dev')
        (root / 'world.mt').unlink()
        (root / 'world.mt').mkdir()
        self.assertEqual(catalogue(self.config)['worlds'], [])

    def test_unreadable_metadata_fails_instead_of_publishing_a_partial_list(self):
        self.world('dev')
        with patch('pathlib.Path.lstat', side_effect=PermissionError('private filesystem path')):
            with self.assertRaises(PermissionError):
                catalogue(self.config)

    def test_symlinks_to_maps_or_settings_are_not_followed(self):
        root = self.world('dev')
        link = self.base / 'link'
        try:
            link.symlink_to(root, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symbolic links requires platform permission')
        self.config['worlds']['linked'] = str(link / 'civicrm_bridge')
        self.assertEqual([item['world_id'] for item in catalogue(self.config)['worlds']], ['dev'])
        (root / 'world.mt').rename(root / 'saved.mt')
        (root / 'world.mt').symlink_to(root / 'saved.mt')
        self.assertEqual(catalogue(self.config)['worlds'], [])

    def test_invalid_operator_configuration_is_rejected(self):
        for worlds in (None, [], {'../escape': str(self.base)}, {'valid': 'relative'},
                       {'valid': []}, {'valid': str(self.base / '..' / 'escape')}):
            with self.subTest(worlds=worlds):
                with self.assertRaises(ValueError):
                    catalogue({'worlds': worlds})


if __name__ == '__main__':
    unittest.main(verbosity=2)
