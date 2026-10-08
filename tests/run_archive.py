#!/usr/bin/env python3
"""Exercise the archive hook using disposable repositories and fictitious content."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REAL_GIT = shutil.which('git')
REAL_GITLEAKS = shutil.which('gitleaks') or str(Path.home() / '.local/bin/gitleaks')
SPEC = importlib.util.spec_from_file_location('archive_checker', ROOT / 'tools/check_archive.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-tests-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'source'
        self.repo.mkdir()
        self.remote = self.base / 'remote.git'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.scans = self.base / 'scans'
        self.trees = self.base / 'trees'
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                        GIT_EXEC_PATH=str(self.bin),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        ARCHIVE_TEST_SCANS=str(self.scans), ARCHIVE_TEST_TREES=str(self.trees),
                        ARCHIVE_TEST_GIT=REAL_GIT)
        # A fixture must not inherit the live checkout's Git environment.
        for key in list(self.env):
            if key in {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                       'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'} or key.startswith('GIT_CONFIG_KEY_') or key.startswith('GIT_CONFIG_VALUE_'):
                self.env.pop(key, None)
        self.env.pop('GIT_CONFIG_COUNT', None)
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Archive Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.hooksPath', 'tools/git-hooks')
        (self.repo / 'tools/git-hooks').mkdir(parents=True)
        shutil.copyfile(ROOT / 'tools/check_archive.py', self.repo / 'tools/check_archive.py')
        hook = self.repo / 'tools/git-hooks/pre-push'
        shutil.copyfile(ROOT / 'tools/git-hooks/pre-push', hook)
        hook.chmod(0o755)
        self.executable('git', r'''import json, os, subprocess, sys
if 'ls-tree' in sys.argv:
    with open(os.environ['ARCHIVE_TEST_TREES'], 'a') as out:
        out.write(json.dumps(sys.argv[-1]) + '\n')
sys.exit(subprocess.call([os.environ['ARCHIVE_TEST_GIT'], *sys.argv[1:]]))
''')
        self.executable('gitleaks', r'''import json, os, subprocess, sys
opts = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--log-opts='))
git = [os.environ['ARCHIVE_TEST_GIT'], '-C', sys.argv[-1]]
commits = subprocess.check_output(git + ['log', '--format=%H', *opts.split()]).decode().splitlines()
with open(os.environ['ARCHIVE_TEST_SCANS'], 'a') as out:
    out.write(json.dumps(commits) + '\n')
content = subprocess.check_output(git + ['log', '-p', *opts.split()])
marker = b'ARCHIVE_' + b'FIXTURE_SECRET'
if marker in content or os.environ.get('ARCHIVE_TEST_FAIL_SCAN'):
    print('scanner diagnostic ' + marker.decode(), file=sys.stderr)
    sys.exit(1)
''')
        (self.repo / 'clean.txt').write_text('fictitious example\n')
        self.git('add', 'clean.txt')
        self.git('commit', '-qm', 'Fixture base')
        self.first = self.git('rev-parse', 'HEAD').stdout.strip()
        subprocess.run([REAL_GIT, 'init', '-q', '--bare', str(self.remote)],
                       env=self.env, check=True, capture_output=True)
        self.git('remote', 'add', 'fixture', str(self.remote))

    def executable(self, name, code):
        target = self.bin / name
        target.write_text('#!' + sys.executable + '\n' + code)
        target.chmod(0o755)

    def git(self, *args, input=None, check=True, env=None):
        return subprocess.run([REAL_GIT, '-C', str(self.repo), *args],
                              env=env or self.env, input=input, capture_output=True,
                              text=True, check=check, timeout=120)

    def dangling(self, name='extra.txt', content='fictitious update\n', parent=None, remove=None):
        # A separate index and commit-tree create no branch, tag or working-tree edit.
        env = dict(self.env, GIT_INDEX_FILE=str(self.base / 'index'))
        self.git('read-tree', parent or self.first, env=env)
        if remove:
            self.git('update-index', '--force-remove', remove, env=env)
        blob = self.git('hash-object', '-w', '--stdin', input=content).stdout.strip()
        self.git('update-index', '--add', '--cacheinfo', '100644', blob, name, env=env)
        tree = self.git('write-tree', env=env).stdout.strip()
        return self.git('commit-tree', tree, '-p', parent or self.first,
                        input='Fictitious dangling commit\n').stdout.strip()

    def push(self, *specs, blocked=False, env=None):
        result = self.git('push', 'fixture', *specs, check=False, env=env)
        self.assertEqual(result.returncode == 0, not blocked,
                         'Unexpected push decision (diagnostics suppressed)')
        if blocked:
            self.assertIn('Archive blocked:', result.stderr)
            self.assertNotIn('scanner diagnostic', result.stderr)
        return result

    def direct(self, records):
        return subprocess.run([sys.executable, '-I', str(self.repo / 'tools/check_archive.py'), '--pre-push'],
                              input=records, env=self.env, capture_output=True,
                              text=True, timeout=120)

    def record(self, oid, remote_oid=None):
        return 'explicit ' + oid + ' refs/heads/example ' + (remote_oid or '0' * len(self.first)) + '\n'

    def assert_scope(self, *pending):
        expected = set(self.git('rev-list', '--all', *pending, '--').stdout.splitlines())
        inspected = {json.loads(line) for line in self.trees.read_text().splitlines()}
        scanned = {oid for line in self.scans.read_text().splitlines() for oid in json.loads(line)}
        self.assertEqual(inspected, expected)
        self.assertEqual(scanned, expected)

    def test_dangling_prohibited_path_is_blocked(self):
        oid = self.dangling('inbox/fictitious.json')
        self.assertNotIn(oid, self.git('rev-list', '--all').stdout)
        self.push(oid + ':refs/heads/blocked-path', blocked=True)
        self.assertFalse(self.git('ls-remote', 'fixture', 'refs/heads/blocked-path').stdout)

    def test_prohibited_path_in_dangling_parent_is_blocked(self):
        parent = self.dangling('receipts/fictitious.json')
        oid = self.dangling(parent=parent, remove='receipts/fictitious.json')
        self.assertNotIn('receipts/fictitious.json', self.git('ls-tree', '-r', '--name-only', oid).stdout)
        self.push(oid + ':refs/heads/blocked-parent', blocked=True)

    def test_dangling_secret_reaches_scanner_and_is_blocked(self):
        oid = self.dangling(content='ARCHIVE_' + 'FIXTURE_SECRET\n')
        self.push(oid + ':refs/heads/blocked-secret', blocked=True)
        self.assert_scope(oid)
        self.assertFalse(self.git('ls-remote', 'fixture', 'refs/heads/blocked-secret').stdout)

    def test_clean_dangling_history_has_identical_scope(self):
        parent = self.dangling()
        oid = self.dangling('next.txt', parent=parent)
        self.push(oid + ':refs/heads/clean-dangling')
        self.assert_scope(oid)

    def test_ordinary_new_branch_force_update_and_deletion(self):
        self.push('main:refs/heads/main')
        oid = self.dangling()
        self.git('update-ref', 'refs/heads/main', oid)
        self.push('main:refs/heads/main', 'main:refs/heads/new')
        self.push('+' + self.first + ':refs/heads/main')
        self.push(':refs/heads/new')
        self.assertFalse(self.git('ls-remote', 'fixture', 'refs/heads/new').stdout)

    def test_all_records_are_scanned(self):
        good = self.dangling()
        bad = self.dangling('world.mt')
        self.push(good + ':refs/heads/good', bad + ':refs/heads/bad', blocked=True)
        self.assertFalse(self.git('ls-remote', 'fixture').stdout)

    def test_annotated_and_lightweight_commit_tags(self):
        oid = self.dangling()
        self.git('tag', '-am', 'Fictitious annotation', 'fixture-tag', oid)
        tag_oid = self.git('rev-parse', 'fixture-tag').stdout.strip()
        self.git('update-ref', '-d', 'refs/tags/fixture-tag')
        self.push(tag_oid + ':refs/tags/annotated', oid + ':refs/tags/lightweight')
        self.assert_scope(oid)

    def test_annotated_tag_to_prohibited_commit_is_blocked(self):
        oid = self.dangling('state.json')
        self.git('tag', '-am', 'Fictitious annotation', 'bad-tag', oid)
        tag_oid = self.git('rev-parse', 'bad-tag').stdout.strip()
        self.git('update-ref', '-d', 'refs/tags/bad-tag')
        self.push(tag_oid + ':refs/tags/bad', blocked=True)

    def test_noncommit_and_missing_objects_are_blocked(self):
        blob = self.git('hash-object', '-w', '--stdin', input='fictitious blob\n').stdout.strip()
        tree = self.git('rev-parse', 'HEAD^{tree}').stdout.strip()
        self.git('tag', '-am', 'Fictitious blob annotation', 'blob-tag', blob)
        tag = self.git('rev-parse', 'blob-tag').stdout.strip()
        for oid in (blob, tree, tag, 'f' * len(self.first)):
            with self.subTest(kind=oid[:4]):
                result = self.direct(self.record(oid))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Archive blocked:', result.stderr)

    def test_malformed_records_are_blocked(self):
        zero = '0' * len(self.first)
        for records in ('\n', 'too few fields\n', self.record('--all'),
                        self.record(self.first, 'bad'), self.record(zero),
                        self.record(self.first).replace('explicit', '(delete)'),
                        self.record(self.first).replace('refs/heads/example', 'invalid..ref')):
            result = self.direct(records)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Archive blocked:', result.stderr)

    def test_unknown_remote_baseline_does_not_reduce_scope(self):
        oid = self.dangling()
        self.assertEqual(self.direct(self.record(oid, 'f' * len(self.first))).returncode, 0)
        self.assert_scope(oid)

    def test_empty_updates_still_audit_local_history(self):
        self.assertEqual(self.direct('').returncode, 0)
        self.assert_scope()

    def test_scan_failure_is_sanitized_and_blocks(self):
        self.push('main:refs/heads/main', blocked=True,
                  env=dict(self.env, ARCHIVE_TEST_FAIL_SCAN='1'))

    def test_scan_batches_use_the_same_frozen_commit_set(self):
        oid = self.dangling()
        with patch.object(checker, 'ROOT', self.repo), patch.object(checker, 'SCAN_BATCH_SIZE', 1), \
             patch.dict(os.environ, self.env, clear=True), patch.object(sys, 'argv', ['check_archive.py', '--pre-push']), \
             patch.object(sys, 'stdin', io.StringIO(self.record(oid))), patch('sys.stdout', io.StringIO()):
            self.assertEqual(checker.main(), 0)
        self.assert_scope(oid)
        self.assertEqual(len(self.scans.read_text().splitlines()), 2)

    def test_missing_scanner_blocks(self):
        with patch.object(checker, 'ROOT', self.repo), patch.dict(os.environ, self.env, clear=True), \
             patch.object(sys, 'argv', ['check_archive.py']), patch.object(checker.shutil, 'which', return_value=None), \
             patch.object(checker.Path, 'home', return_value=self.base / 'missing-home'), patch('sys.stderr', io.StringIO()) as errors:
            self.assertEqual(checker.main(), 1)
            self.assertIn('install Gitleaks', errors.getvalue())

    def test_shallow_checkout_blocks(self):
        (self.repo / '.git/shallow').write_text(self.first + '\n')
        result = self.direct(self.record(self.first))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Archive blocked:', result.stderr)

    @unittest.skipUnless(Path(REAL_GITLEAKS).is_file(), 'Gitleaks is required for the real-scanner fixture')
    def test_real_gitleaks_blocks_fictitious_dangling_secret(self):
        config = self.base / 'scanner.toml'
        marker = 'ARCHIVE_' + 'FIXTURE_SECRET'
        config.write_text('[[rules]]\nid = "archive-fixture"\ndescription = "Fictitious test marker"\nregex = "' + marker + '"\n')
        (self.bin / 'gitleaks').unlink()
        (self.bin / 'gitleaks').symlink_to(REAL_GITLEAKS)
        env = dict(self.env, GITLEAKS_CONFIG=str(config))
        good = self.dangling()
        self.push(good + ':refs/heads/real-clean', env=env)
        parent = self.dangling('marker.txt', content=marker + '\n')
        bad = self.dangling(parent=parent, remove='marker.txt')
        self.push(bad + ':refs/heads/real-blocked', blocked=True, env=env)
        self.assertFalse(self.git('ls-remote', 'fixture', 'refs/heads/real-blocked').stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
