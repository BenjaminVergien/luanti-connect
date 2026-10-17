#!/usr/bin/env python3
"""Prepare reviewable deployment files; administrator setup is deliberately manual."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.security import check_parents

LIB = Path('/usr/local/lib/civicrm-bridge-api')
ETC = Path('/etc/civicrm-bridge-api')
STATE = Path('/var/lib/civicrm-bridge-api')
USER = 'civicrm_bridge_api'


def hostname(value):
    if (len(value) > 253 or '.' not in value
            or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                   for label in value.split('.'))
            or value.endswith(('.localhost', '.local', '.internal', '.home.arpa'))):
        raise ValueError('Supply a public DNS hostname, without a scheme, path or port')
    try:
        import ipaddress
        ipaddress.ip_address(value)
    except ValueError:
        return value.lower()
    raise ValueError('Supply a DNS hostname, not an IP address')


def unit(venv):
    return f'''[Unit]
Description=Authenticated Luanti bridge API
After=network.target systemd-tmpfiles-setup.service

[Service]
Type=simple
User={USER}
Group={USER}
UMask=0077
ExecStart={venv}/bin/python -I {LIB}/api/serve.py
Restart=on-failure
RestartSec=3
TimeoutStopSec=620
KillMode=control-group
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths={STATE} /var/lib/civicrm-worldctl /run/civicrm-worldctl.lock /home/ben/luanti-dev/instances
# sudo must retain its setuid transition for the exact root-owned controller.
# NoNewPrivileges=true would disable that transition.

[Install]
WantedBy=multi-user.target
'''.encode()


def deployment_files(domain, owner_uid):
    config = {'hostname': hostname(domain), 'token_file': str(ETC / 'token.json'),
              'state_dir': str(STATE), 'world_owner_uid': owner_uid,
              'templates': {}, 'instances_root': '/home/ben/luanti-dev/instances',
              'instance_registry': '/var/lib/civicrm-worldctl/instances',
              'worlds': {'dev': '/home/ben/luanti-dev/world/civicrm_bridge',
                         'test': '/home/ben/luanti-dev/worlds/test/civicrm_bridge'}}
    commands = ('status', 'stop_world', 'start_world dev', 'start_world test', 'switch_world dev', 'switch_world test', 'instance', 'reconcile_instances')
    sudoers = f'{USER} ALL=(root) NOPASSWD: ' + ', '.join('/usr/local/sbin/civicrm-worldctl ' + x for x in commands) + '\n'
    wrapper = '#!/bin/sh\nexec ' + str(LIB) + '/venv/bin/python -I ' + str(LIB) + '/api/token_cli.py "$@"\n'
    launcher = ('import sys\nsys.path.insert(0, "/usr/local/lib/civicrm-bridge-api")\n'
                'from api.tokens import main\n'
                'try:\n    main()\nexcept (OSError, ValueError, KeyError, TypeError):\n'
                '    raise SystemExit("Token operation failed; inspect paths and permissions.")\n')
    return {'config.json': (json.dumps(config, indent=2) + '\n').encode(),
            'civicrm-bridge-api.service': unit(LIB / 'venv'),
            'civicrm-bridge-api.sudoers': sudoers.encode(),
            'civicrm-bridge-api.tmpfiles': b'f /run/civicrm-worldctl.lock 0600 root root -\n',
            'civicrm-bridge-token': wrapper.encode(), 'token_cli.py': launcher.encode()}


def main():
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', required=True, type=hostname)
    parser.add_argument('--output', required=True, type=Path, help='New private directory outside the Git checkout')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Prepare these files without sudo; perform reviewed setup commands manually')
    output = args.output.absolute()
    check_parents(output)
    if output == ROOT or ROOT in output.parents:
        parser.error('Deployment configuration must remain outside Git')
    files = deployment_files(args.hostname, pwd.getpwnam('ben').pw_uid)
    os.umask(0o077)
    output.mkdir(mode=0o700)
    for name, raw in files.items():
        with (output / name).open('xb') as target:
            target.write(raw)
    print('Prepared configuration and service files for review. No account, service, proxy or token was changed.')
    print('Follow docs/HTTPS_API.md from the ben session to complete manual setup.')


if __name__ == '__main__':
    main()
