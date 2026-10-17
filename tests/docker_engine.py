#!/usr/bin/env python3
"""Engine adapter for legacy regression fixtures, confined to one test run.

This is test infrastructure, never a production backend. Legacy fixtures run as
root inside their disposable container; run_docker_integration checks production
UID, mounts and permissions separately.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid


def main():
    root = Path(os.environ['BRIDGE_TEST_ROOT']).resolve(strict=True)
    source = Path(__file__).resolve().parents[1]
    game = Path(os.environ['BRIDGE_TEST_GAME']).resolve(strict=True)
    image = os.environ['BRIDGE_TEST_IMAGE']
    arguments = sys.argv[1:]
    world = Path(arguments[arguments.index('--world') + 1]).resolve(strict=True)
    home = Path(os.environ['HOME']).resolve(strict=True)
    if root not in world.parents or (home != root and root not in home.parents):
        raise ValueError('Engine fixture escaped the isolated run directory')
    name = 'bridge-fixture-' + uuid.uuid4().hex
    command = ['/usr/bin/docker', '--host=unix:///var/run/docker.sock']
    create = [*command, 'create', '--name', name, '--pull=never', '--read-only', '--network=host',
              '--label=bridge-test-run=' + root.name, '--cap-drop=ALL', '--security-opt=no-new-privileges',
              '--user=0:0', '--entrypoint=/bin/sh', '--env=HOME=' + str(home), '--tmpfs=/tmp:rw,nosuid,size=64m',
              '--tmpfs=/var/lib/minetest:rw,nosuid,size=4m', '--tmpfs=/etc/minetest:ro,nosuid,size=1m']
    for path, writable in ((root, True), (source, False), (game, False)):
        create += ['--mount', 'type=bind,src=' + str(path) + ',dst=' + str(path) + ('' if writable else ',readonly')]
    identifier = subprocess.check_output([*create, image, '-c', 'umask 0077; exec /usr/local/bin/luantiserver "$@"', 'luanti',
                                          *[arg for arg in arguments if arg != '--server']], timeout=20).decode().strip()

    def stop(*_):
        subprocess.run([*command, 'stop', '--time', '5', identifier], capture_output=True, timeout=10, check=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return subprocess.call([*command, 'start', '--attach', identifier])
    finally:
        remote = json.loads(subprocess.check_output([*command, 'inspect', identifier], timeout=10))[0]
        if remote['Id'] == identifier and remote['Config']['Labels'].get('bridge-test-run') == root.name:
            subprocess.run([*command, 'rm', '-f', identifier], check=True, capture_output=True, timeout=15)


if __name__ == '__main__':
    sys.exit(main())
