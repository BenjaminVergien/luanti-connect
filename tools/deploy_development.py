#!/usr/bin/env python3
"""Run isolated Linux checks or install the exact tested development snapshot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.security import atomic_private, read_regular
from lifecycle.install_docker import install, directory, command, encoded

BASE = Path('/var/lib/luanti-portal/bridge-development')


def run_suite(arguments, environment, log):
    with log.open('ab') as output:
        result = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, timeout=300, check=False)
    if log.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Test diagnostics exceeded their bound')
    if result.returncode:
        raise ValueError('Linux test failed: ' + Path(arguments[1]).name + '; private diagnostics: ' + str(log))


def test(settings):
    manifest = json.loads((ROOT / 'bundle.json').read_text())
    directory(BASE)
    run = BASE / ('run-' + uuid.uuid4().hex)
    directory(run, 0o700)
    fixture = run / 'fixtures'
    directory(fixture, 0o755)
    venv = run / 'venv'
    command('/usr/bin/python3', '-m', 'venv', str(venv))
    command(str(venv / 'bin/python'), '-m', 'pip', 'install', '--only-binary=:all:', '-r', str(ROOT / 'api/requirements.txt'), 'httpx==0.28.1', timeout=300)
    executable = str(venv / 'bin/python')
    image = json.loads(subprocess.check_output(['/usr/bin/docker', 'image', 'inspect', settings['image']], timeout=15))[0]['Id']
    environment = dict(os.environ, HOME=str(fixture), PYTHONDONTWRITEBYTECODE='1',
                       BRIDGE_TEST_ROOT=str(fixture), BRIDGE_TEST_GAME=settings['game_source'], BRIDGE_TEST_IMAGE=image)
    adapter = ROOT / 'tests/docker_engine.py'
    adapter.chmod(0o755)
    directory(run / 'bin', 0o755)
    (run / 'bin/luanti').symlink_to(adapter)
    environment['PATH'] = str(run / 'bin') + os.pathsep + os.environ['PATH']
    environment['CADDY_BIN'] = '/usr/bin/caddy'
    suites = [
        ['run_docker.py'], ['run_docker_installer.py'], ['run_instances.py'], ['run_templates.py'], ['run_upgrade.py'],
        ['run_world_catalogue.py'], ['run_world_security.py'], ['run_world_worker.py'], ['run_api.py'],
        ['run_lifecycle.py', '--mock-only', '--base', str(fixture), '--engine', str(adapter), '--game-path', settings['game_source']],
        ['run_docker_integration.py', '--image', image, '--game-path', settings['game_source'], '--base', str(fixture)],
        ['run_integration.py', '--engine', str(adapter), '--base', str(fixture), '--game-path', settings['game_source']],
        ['run_examples.py', '--engine', str(adapter), '--base', str(fixture), '--game-path', settings['game_source']],
        ['run_lifecycle.py', '--engine', str(adapter), '--base', str(fixture), '--game-path', settings['game_source']],
        ['run_instances.py', '--real-engine', '--engine', str(adapter), '--game-path', settings['game_source']],
    ]
    completed = []
    for suite in suites:
        run_suite([executable, str(ROOT / 'tests' / suite[0]), *suite[1:]], environment, run / 'tests.log')
        completed.append(' '.join(suite[:2]))
    receipt = {'version': 1, 'source_sha256': manifest['sha256'], 'game_sha256': settings['game_sha256'],
               'image_id': image, 'completed': completed, 'run': run.name}
    directory(BASE / 'tested', 0o700)
    atomic_private(BASE / 'tested' / (manifest['sha256'] + '.json'), encoded(receipt), owner=0, group=0)
    return dict(receipt, ok=True)


def tls():
    path = Path('/etc/civicrm-bridge-api/development-tls')
    directory(path, 0o750)
    group = pwd.getpwnam('civicrm_bridge_api').pw_gid
    os.chown(path, 0, group)
    if not (path / 'ca.crt').exists():
        command('/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-days', '3650',
                '-subj', '/CN=Luanti private development CA', '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-keyout', str(path / 'ca.key'), '-out', str(path / 'ca.crt'))
        command('/usr/bin/openssl', 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN=localhost',
                '-keyout', str(path / 'server.key'), '-out', str(path / 'server.csr'))
        (path / 'extensions').write_text('subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\n')
        command('/usr/bin/openssl', 'x509', '-req', '-in', str(path / 'server.csr'), '-CA', str(path / 'ca.crt'),
                '-CAkey', str(path / 'ca.key'), '-CAcreateserial', '-days', '365', '-sha256',
                '-extfile', str(path / 'extensions'), '-out', str(path / 'server.crt'))
    for name in ('ca.key', 'ca.srl', 'server.csr', 'extensions'):
        (path / name).chmod(0o600)
    for name in ('ca.crt', 'server.crt', 'server.key'):
        (path / name).chmod(0o640)
        os.chown(path / name, 0, group)
    command('/usr/bin/systemctl', 'daemon-reload')
    command('/usr/bin/systemctl', 'enable', '--now', 'civicrm-bridge-development-proxy.service')
    return read_regular(path / 'ca.crt', 16384, {0}).decode()


def up(settings):
    manifest = json.loads((ROOT / 'bundle.json').read_text())
    receipt = json.loads(read_regular(BASE / 'tested' / (manifest['sha256'] + '.json'), 65536, {0}, forbidden=0o077))
    current_image = json.loads(subprocess.check_output(['/usr/bin/docker', 'image', 'inspect', settings['image']], timeout=15))[0]['Id']
    if receipt['game_sha256'] != settings['game_sha256'] or receipt['image_id'] != current_image:
        raise ValueError('Game content or image changed after testing; run bridge test again')
    install_settings = {key: value for key, value in settings.items() if key != 'game_sha256'}
    install(install_settings)
    result = install(install_settings, apply=True)
    ca = tls()
    atomic_private(BASE / 'active.json', encoded({'version': 1, 'source': str(ROOT), 'settings': settings}), owner=0, group=0)
    token = read_regular(Path('/etc/civicrm-bridge-api/development.token'), 4096, {0}, forbidden=0o077).decode().strip()
    return {'version': 1, 'ok': True, 'source_sha256': manifest['sha256'], 'image_id': result['image_id'], 'ca_certificate': ca, 'token': token}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('test', 'up'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = json.loads(read_regular(args.config, 65536, {0}, forbidden=0o077))
        print(json.dumps(test(settings) if args.operation == 'test' else up(settings)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'error': str(error) if isinstance(error, ValueError) else 'Private development operation failed; inspect remote diagnostics.'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
