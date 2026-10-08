#!/usr/bin/env python3
"""Systemd launches this as ben; the engine replaces this process."""
import os
from pathlib import Path
import sys

sys.path.insert(0, '/usr/local/lib/civicrm-worldctl')
from controller import LifecycleController, LifecycleError, launch_arguments, trusted_json


def main():
    try:
        config = trusted_json('/etc/civicrm-worldctl/config.json')
        state = trusted_json('/var/lib/civicrm-worldctl/state.json')
        expected_uid = Path(config['user_home']).stat().st_uid
        if os.geteuid() != expected_uid or os.geteuid() == 0:
            raise LifecycleError('wrong_user', 'Luanti launcher must run as the configured non-root service user.')
        world_id = state.get('world_id')
        controller = LifecycleController(config, None, Path('/var/lib/civicrm-worldctl/state.json'),
                                         Path('/run/civicrm-worldctl.lock'))
        controller.validate_world(world_id)
        arguments = launch_arguments(config, world_id)
        environment = dict(os.environ)
        environment['HOME'] = config['user_home']
        os.execve(arguments[0], arguments, environment)
    except (LifecycleError, OSError, ValueError, KeyError):
        print('civicrm-worldctl: invalid launch configuration or world; refusing startup.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
