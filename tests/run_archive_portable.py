#!/usr/bin/env python3
"""Execute the real pre-push hook on Windows and Linux with disposable repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArchiveHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-hook-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'source'
        self.repo.mkdir()
        self.environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        for key in list(self.environment):
            if key.startswith('GIT_') and key != 'GIT_TERMINAL_PROMPT':
                self.environment.pop(key)
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Archive fixture')
        self.git('config', 'user.email', 'archive@example.invalid')
        self.git('config', 'core.hooksPath', 'tools/git-hooks')
        for name in ('tools/check_archive.py', 'tools/git-hooks/pre-push'):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
            target.chmod(0o755)
        subprocess.run(['git', 'init', '--bare', '-q', str(self.base / 'remote.git')], check=True, capture_output=True)
        self.git('remote', 'add', 'fixture', str(self.base / 'remote.git'))
        (self.repo / 'example.txt').write_text('Fictitious safe example\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Archive fixture')

    def git(self, *arguments):
        return subprocess.run(['git', '-C', str(self.repo), *arguments], env=self.environment,
                              capture_output=True, timeout=120, check=True)

    def test_clean_push_executes_python_hook_and_mandatory_scanner(self):
        result = self.git('push', 'fixture', 'main')
        self.assertIn(b'Archive check passed', result.stderr + result.stdout)

    def test_local_configuration_is_rejected_before_push(self):
        (self.repo / '.env').write_text('FICTITIOUS_SETTING=example\n')
        self.git('add', '.env')
        self.git('commit', '-qm', 'Forbidden fixture path')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.git('push', 'fixture', 'main')
        self.assertIn(b'Archive blocked', failure.exception.stderr)

    def test_scanner_rejection_is_sanitized_and_cannot_be_bypassed(self):
        marker = 'PORTABLE_' + 'ARCHIVE_FIXTURE_MARKER'
        config = self.base / 'scanner.toml'
        config.write_text('[[rules]]\nid="portable-fixture"\ndescription="Fictitious marker"\nregex="' + marker + '"\n')
        self.environment['GITLEAKS_CONFIG'] = str(config)
        (self.repo / 'example.txt').write_text(marker)
        self.git('add', 'example.txt')
        self.git('commit', '-qm', 'Fictitious scanner fixture')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.git('push', 'fixture', 'main')
        self.assertIn(b'Archive blocked', failure.exception.stderr)
        self.assertNotIn(marker.encode(), failure.exception.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
