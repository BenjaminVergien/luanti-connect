#!/usr/bin/env python3
"""Launch the root-owned installed snapshot, never imports from the working tree."""
import argparse
import json
import os
from pathlib import Path
import sys

# Explicitly restore only the reviewed install root when launched with python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.app import create_app
from api.security import read_regular


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/civicrm-bridge-api/config.json'))
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(read_regular(args.config, 16384, {0}, forbidden=0o022))
    if not isinstance(config.get('worlds'), dict) or (config.get('backend', 'systemd') != 'docker' and set(config['worlds']) != {'dev', 'test'}):
        raise ValueError('Invalid world configuration')
    import uvicorn
    uvicorn.run(create_app(config), host='127.0.0.1', port=8765, workers=1,
                proxy_headers=False, access_log=False, limit_concurrency=64,
                timeout_keep_alive=5, timeout_graceful_shutdown=610)


if __name__ == '__main__':
    main()
