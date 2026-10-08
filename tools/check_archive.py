#!/usr/bin/env python3
"""Check every reachable commit before archiving; never print secret matches."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def sensitive_path(name):
    parts = Path(name).parts
    leaf = parts[-1] if parts else ''
    return (any(part in {'.ssh', 'secrets', 'world', 'worlds', 'inbox', 'outbox', 'receipts'} for part in parts)
            or leaf in {'server.conf', 'world.mt', 'state.json'}
            or leaf == '.env' or leaf.startswith('.env.')
            or leaf.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa', 'gitleaks-report'))
            or leaf.endswith(('.pem', '.key', '.log', '.db'))
            or '.sqlite' in leaf or '.db-' in leaf)


def main():
    environment = dict(os.environ, GIT_CONFIG_COUNT='1',
                       GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(ROOT))
    git = ['git', '-C', str(ROOT)]
    try:
        commits = subprocess.check_output(git + ['rev-list', '--all'], env=environment).splitlines()
        for commit in commits:
            names = subprocess.check_output(git + ['ls-tree', '-r', '--name-only', '-z', commit.decode()],
                                            env=environment).decode().split('\0')
            if any(sensitive_path(name) for name in names if name):
                print('Archive blocked: a commit tracks a credential, local configuration or runtime file.', file=sys.stderr)
                return 1
        executable = shutil.which('gitleaks')
        if executable is None:
            candidate = Path.home() / '.local/bin/gitleaks'
            executable = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        if executable is None:
            print('Archive blocked: install Gitleaks before pushing.', file=sys.stderr)
            return 1
        result = subprocess.run([executable, 'git', '--redact=100', '--no-banner',
                                 '--log-opts=--all', '--log-level=error', str(ROOT)],
                                env=environment, capture_output=True, timeout=120)
        if result.returncode:
            # Do not echo scanner diagnostics: matches can contain credentials.
            print('Archive blocked: the secret scan failed. Inspect locally using Gitleaks with --redact=100.', file=sys.stderr)
            return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Archive blocked: checks could not complete.', file=sys.stderr)
        return 1
    print('Archive check passed: history file boundaries and secret scan.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
