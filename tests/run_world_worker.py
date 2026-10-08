#!/usr/bin/env python3
"""Exercise JSON dispatch, receipts, concurrency and process failures without live world changes."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import world_worker as worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'state'
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def raw(self, operation='status', job_id='test-status', **fields):
        return json.dumps(dict(version=1, job_id=job_id, operation=operation, **fields)).encode()

    def runner(self, arguments):
        self.calls.append(arguments)
        return {'ok': True, 'state': 'running', 'ready': True, 'world_id': 'dev'}

    def handle(self, raw):
        return worker.handle(raw, self.state, self.runner)

    def test_all_published_requests_map_to_fixed_arguments(self):
        for source in (ROOT / 'examples/lifecycle/proposed-requests').glob('*.json'):
            raw = source.read_bytes()
            request = json.loads(raw)
            result = self.handle(raw)
            self.assertTrue(result['ok'])
            self.assertEqual(result['job_id'], request['job_id'])
            self.assertEqual(self.calls[-1], [*worker.COMMAND, request['operation']]
                             + ([request['world_id']] if 'world_id' in request else []))
        self.assertEqual(len(self.calls), 6)

    def test_invalid_requests_never_invoke_controller(self):
        invalid = [b'[]', b'null', b'{', b'\xff', b' ' * (worker.MAX_BYTES + 1),
                   b'{"version":1,"version":1,"job_id":"a","operation":"status"}',
                   self.raw(world_id='dev'), self.raw('stop_world', world_id='test'),
                   self.raw('start_world'), self.raw('start_world', world_id='/tmp/world'),
                   self.raw('switch_world', world_id='test; id'),
                   self.raw('sync_accounts'), self.raw(job_id='../a'), self.raw(extra='x')]
        for version in (True, 1.0, '1', None, 2):
            invalid.append(json.dumps(dict(version=version, job_id='a', operation='status')).encode())
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.handle(raw)['error']['code'], 'invalid_request')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state.exists())

    def test_receipt_replay_survives_new_worker_and_conflicts(self):
        raw = self.raw('switch_world', world_id='test')
        first = self.handle(raw)
        self.assertEqual(self.handle(raw), first)
        self.assertEqual(worker.handle(raw, self.state, lambda args: self.fail('Reexecuted')), first)
        self.assertEqual(self.handle(raw + b'\n')['error']['code'], 'job_id_conflict')
        self.assertEqual(self.handle(self.raw())['error']['code'], 'job_id_conflict')
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_dispatch_is_not_replayed(self):
        def interrupted(arguments):
            self.calls.append(arguments)
            raise RuntimeError('simulated crash')
        raw = self.raw('stop_world')
        with self.assertRaises(RuntimeError):
            worker.handle(raw, self.state, interrupted)
        self.assertEqual(self.handle(raw)['error']['code'], 'outcome_unknown')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.handle(self.raw(job_id='fresh-status'))['ok'])

    def test_concurrency_busy_does_not_consume_id(self):
        self.handle(self.raw(job_id='seed'))
        with open(self.state / 'worker.lock', 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.handle(self.raw())['error']['code'], 'busy')
        self.assertTrue(self.handle(self.raw())['ok'])

    def test_unsafe_state_and_symlinks_are_rejected(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        self.assertEqual(self.handle(self.raw())['error']['code'], 'worker_state_error')
        self.state.chmod(0o700)
        target = Path(self.temp.name) / 'target'
        target.write_text('untouched')
        (self.state / 'receipts.sqlite3').symlink_to(target)
        self.assertEqual(self.handle(self.raw())['error']['code'], 'worker_state_error')
        self.assertEqual(target.read_text(), 'untouched')
        self.assertEqual(self.calls, [])

    def test_controller_errors_preserve_rollback_details_and_replay(self):
        failure = {'ok': False, 'error': {'code': 'switch_failed', 'message': 'Failed',
                   'details': {'rollback': 'restored', 'previous_world_id': 'dev', 'cause': 'startup_timeout'}}}
        raw = self.raw('switch_world', world_id='test')
        result = worker.handle(raw, self.state, lambda args: failure)
        self.assertEqual(result['error'], failure['error'])
        self.assertEqual(self.handle(raw), result)
        self.assertEqual(self.calls, [])

    def test_process_contract(self):
        cases = [(0, b'{"ok":true,"state":"running"}', None),
                 (1, b'{"ok":false,"error":{"code":"configuration_error","message":"Bad"}}', 'configuration_error'),
                 (1, b'', 'controller_process_error'), (2, b'usage', 'controller_process_error'),
                 (0, b'{"ok":false}', 'controller_process_error'),
                 (1, b'{"ok":true}', 'controller_process_error'),
                 (0, b'null', 'controller_process_error')]
        for code, stdout, error in cases:
            with patch.object(worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, b'private stderr')) as run:
                result = worker.invoke([*worker.COMMAND, 'status'])
                self.assertEqual(result.get('error', {}).get('code'), error)
                self.assertNotIn('private stderr', json.dumps(result))
                self.assertNotIn('shell', run.call_args.kwargs)
        with patch.object(worker.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 600)):
            self.assertEqual(worker.invoke([])['error']['code'], 'outcome_unknown')
        with patch.object(worker.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertEqual(worker.invoke([])['error']['code'], 'launch_failed')

    def test_cli_invalid_input_returns_json_and_exit_one(self):
        process = subprocess.run([sys.executable, str(ROOT / 'tools/world_worker.py'),
                                  '--state-dir', str(self.state)], input=b'{}', capture_output=True)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)['error']['code'], 'invalid_request')
        self.assertEqual(process.stderr, b'')


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main(verbosity=2)
