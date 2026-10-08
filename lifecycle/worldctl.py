#!/usr/bin/env python3
"""Restricted entrypoint installed outside the writable development repository."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/usr/local/lib/civicrm-worldctl')
from controller import LifecycleController, LifecycleError, SystemdBackend, trusted_json

CONFIG = Path('/etc/civicrm-worldctl/config.json')
STATE = Path('/var/lib/civicrm-worldctl/state.json')
LOCK = Path('/run/civicrm-worldctl.lock')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('status')
    operations.add_parser('stop_world')
    for operation in ('start_world', 'switch_world'):
        operations.add_parser(operation).add_argument('world_id')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise LifecycleError('permission_denied', 'Run the installed command through its restricted sudo rule.')
        config = trusted_json(CONFIG)
        trusted_json(STATE)
        controller = LifecycleController(config, SystemdBackend(config), STATE, LOCK)
        method = getattr(controller, args.operation)
        result = method(args.world_id) if hasattr(args, 'world_id') else method()
        print(json.dumps(result, indent=2))
        return 0
    except LifecycleError as error:
        print(json.dumps({'ok': False, 'error': {'code': error.code,
                         'message': error.message, 'details': error.details}}, indent=2))
        return 1
    except (OSError, ValueError, KeyError):
        print(json.dumps({'ok': False, 'error': {'code': 'configuration_error',
                         'message': 'Invalid lifecycle installation; administrator inspection required.'}}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
