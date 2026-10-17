#!/usr/bin/env python3
"""Verify published JSON examples with an isolated engine and fake lifecycle service."""
import argparse
import fcntl
import json
from pathlib import Path
import shutil
import sys
import tempfile

from run_integration import Harness, SECRETS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lifecycle.controller import LifecycleController, LifecycleError


def read(path):
    return json.loads(path.read_text())


def check_account_examples(h):
    h.start()
    original = None
    player_password = None
    for source in sorted((ROOT / 'examples').glob('[0-9][0-9]-*.json')):
        raw = source.read_bytes().decode('utf-8')
        request = json.loads(raw)
        for account in request['accounts']:
            if 'initial_password' in account:
                SECRETS.append(account['initial_password'])
        expected = read(ROOT / 'examples/expected' / source.name)
        assert h.submit(request['job_id'], raw=raw) == expected, source.name
        if source.name == '01-create.json':
            original = raw
            player_password = h.snapshot('demo_player')['password']
        for account in request['accounts']:
            snapshot = h.snapshot(account['username'])
            assert snapshot['exists'], source.name
            assert bool(snapshot['denial']) == (not account['admitted']), source.name
            assert bool(snapshot['privileges'].get('fly')) == (account['role'] == 'facilitator'), source.name
        assert h.snapshot('demo_player')['password'] == player_password, 'Example changed existing player password'

    names = ('demo_player', 'demo_facilitator', 'demo_waitlisted')
    before = {name: h.snapshot(name) for name in names}
    assert h.submit('demo-001-create', raw=original) == read(ROOT / 'examples/expected/01-create.json')
    assert {name: h.snapshot(name) for name in names} == before, 'Old creation replay changed current decisions'

    password = 'DemoOnly-Unmanaged!'
    SECRETS.append(password)
    h.control_command('create_account', username='local_account', password=password)
    unmanaged_password = h.snapshot('local_account')['password']
    for source in sorted((ROOT / 'examples/errors/requests').glob('*.json')):
        raw = source.read_bytes().decode('utf-8')
        request = json.loads(raw)
        for account in request['accounts']:
            if 'initial_password' in account:
                SECRETS.append(account['initial_password'])
        assert h.submit(request['job_id'], raw=raw) == read(ROOT / 'examples/errors/responses' / source.name), source.name
        assert {name: h.snapshot(name) for name in names} == before, 'Rejection changed existing example state'
    assert h.snapshot('local_account')['password'] == unmanaged_password
    assert h.snapshot('demo_partial')['exists']
    assert not h.snapshot('demo_no_password')['exists']
    h.stop()
    h.start()
    assert h.submit('demo-001-create', raw=original) == read(ROOT / 'examples/expected/01-create.json')
    assert {name: h.snapshot(name) for name in names} == before, 'Replay after restart changed current decisions'
    h.stop()
    for path in [h.path / 'engine.log', h.path / 'process.log', *h.runtime.rglob('*.json')]:
        if path.exists():
            contents = path.read_text(errors='replace')
            assert not any(secret in contents for secret in SECRETS), 'Example password appeared in retained runtime data'
    print('PASS: all account examples and exact response fixtures, password preservation, replay, errors and restart')


class FixtureBackend:
    """No systemd or real processes; deterministic PIDs match documentation fixtures."""
    def __init__(self, config, selection, current=None, ready=True, fail_world=None, fail_cleanup=False):
        self.config, self.selection = config, selection
        self.current, self.ready = current, ready
        self.fail_world, self.fail_cleanup = fail_world, fail_cleanup

    def info(self):
        if self.current == 'foreign':
            return {'active': True, 'pid': 4244, 'world_path': '/unconfigured/world', 'ready': False}
        return {'active': self.current is not None,
                'pid': {'dev': 4242, 'test': 4243}.get(self.current, 0),
                'world_path': self.config['worlds'].get(self.current),
                'ready': self.current is not None and self.ready}

    def start(self, world_id=None, world_path=None):
        self.current = read(self.selection)['world_id']
        self.ready = self.current != self.fail_world

    def stop(self):
        if self.fail_cleanup and self.current == self.fail_world:
            raise LifecycleError('service_error', 'Fixture cleanup failure')
        self.current = None


def check_lifecycle_examples():
    with tempfile.TemporaryDirectory(prefix='civicrm-example-controls-') as directory:
        base = Path(directory)
        shared = base / 'home/.minetest/mods/civicrm_bridge'
        shared.mkdir(parents=True)
        (shared / 'mod.conf').write_text('name = civicrm_bridge\n')
        config_file = base / 'server.conf'
        config_file.touch()
        config = {'worlds': {name: str(base / name) for name in ('dev', 'test')},
                  'game_id': 'minetest', 'engine': '/usr/bin/true',
                  'user_home': str(base / 'home'), 'server_config': str(config_file)}
        for path in config['worlds'].values():
            Path(path).mkdir(mode=0o700)
            (Path(path) / 'world.mt').write_text('gameid = minetest\nload_mod_civicrm_bridge = mods/civicrm_bridge\n')
            (Path(path) / 'world.mt').chmod(0o600)
        selection, lock = base / 'selection.json', base / 'operation.lock'
        examples = ROOT / 'examples/lifecycle'

        def check(response, operation='status', target=None, current='dev', selected='dev', ready=True,
                  fail_world=None, fail_cleanup=False, busy=False):
            selection.write_text(json.dumps({'world_id': selected}))
            backend = FixtureBackend(config, selection, current, ready, fail_world, fail_cleanup)
            controller = LifecycleController(config, backend, selection, lock, timeout=.02, poll_interval=.001)
            held = lock.open('a') if busy else None
            if held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                try:
                    method = getattr(controller, operation)
                    result = method(target) if target is not None else method()
                except LifecycleError as error:
                    result = {'ok': False, 'error': {'code': error.code, 'message': error.message, 'details': error.details}}
                assert result == read(examples / 'responses' / (response + '.json')), response
            finally:
                if held:
                    held.close()

        for source in (examples / 'proposed-requests').glob('*.json'):
            request = read(source)
            assert request['version'] == 1 and isinstance(request['job_id'], str)
            operation = request['operation']
            assert operation in ('status', 'stop_world', 'start_world', 'switch_world')
            target = request.get('world_id')
            assert target in ('dev', 'test') if operation in ('start_world', 'switch_world') else target is None
            response = 'status-running-dev' if operation == 'status' else source.stem
            current = None if operation == 'start_world' else ('test' if target == 'dev' else 'dev')
            check(response, operation, target, current=current)
        check('status-starting-test', current='test', selected='test', ready=False)
        check('status-stopped', current=None)
        check('status-unmanaged', current='foreign')
        check('stop-already-stopped', 'stop_world', current=None)
        check('start-same-world', 'start_world', 'dev')
        check('switch-same-world', 'switch_world', 'dev')
        check('already-running', 'start_world', 'test')
        check('busy', busy=True)
        check('switch-failed-restored', 'switch_world', 'test', fail_world='test')
        check('start-failed-selection-restored', 'start_world', 'test', current=None, fail_world='test')
        check('switch-failed-rollback-failed', 'switch_world', 'test', fail_world='test', fail_cleanup=True)
    print('PASS: all lifecycle response fixtures match current controller behavior; run run_world_worker.py for JSON worker dispatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', default=shutil.which('luanti') or shutil.which('minetest'))
    parser.add_argument('--base', type=Path, default=Path.home())
    parser.add_argument('--game-path', type=Path, default=Path('/home/ben/.minetest/games/minetest_game'))
    args = parser.parse_args()
    if not args.engine:
        parser.error('Luanti not found; pass --engine')
    for source in (ROOT / 'examples').rglob('*.json'):
        read(source)
    check_lifecycle_examples()
    harness = Harness(args.engine, ROOT, args.base, args.game_path)
    success = False
    try:
        check_account_examples(harness)
        success = True
    finally:
        harness.stop()
        if success:
            shutil.rmtree(harness.path)
        else:
            print('Test artifacts:', harness.path)


if __name__ == '__main__':
    main()
