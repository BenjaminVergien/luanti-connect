#!/usr/bin/env python3
"""Check archive history and exact pending push commits; never print secret matches."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCAN_BATCH_SIZE = 128


def sensitive_path(name):
    parts = PurePosixPath(name).parts
    leaf = parts[-1] if parts else ''
    return (any(part in {'.ssh', 'secrets', 'world', 'worlds', 'inbox', 'outbox', 'receipts'} for part in parts)
            or leaf in {'server.conf', 'world.mt', 'state.json'}
            or leaf == '.env' or leaf.startswith('.env.')
            or leaf.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa', 'gitleaks-report'))
            or leaf.endswith(('.pem', '.key', '.log', '.db'))
            or '.sqlite' in leaf or '.db-' in leaf)


def git_output(git, environment, *arguments):
    return subprocess.check_output(git + list(arguments), env=environment,
                                   stderr=subprocess.PIPE, timeout=120)


def pending_commits(stream, git, environment):
    """Validate every pre-push record, skipping deletions and peeling commit tags."""
    object_format = git_output(git, environment, 'rev-parse', '--show-object-format').strip()
    width = {b'sha1': 40, b'sha256': 64}.get(object_format)
    if width is None:
        raise ValueError('Unsupported object format')
    pattern = re.compile('[0-9a-f]{' + str(width) + '}')
    zero = '0' * width
    commits = set()
    for line in stream:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError('Malformed pre-push record')
        local_ref, local_oid, remote_ref, remote_oid = fields
        if not pattern.fullmatch(local_oid) or not pattern.fullmatch(remote_oid):
            raise ValueError('Malformed pre-push object ID')
        # Never pass ref expressions from stdin as command-line revisions.
        git_output(git, environment, 'check-ref-format', remote_ref)
        if local_oid == zero:
            if local_ref != '(delete)' or remote_oid == zero:
                raise ValueError('Malformed deletion')
            continue
        if local_ref == '(delete)':
            raise ValueError('Malformed update')
        kind = git_output(git, environment, 'cat-file', '-t', local_oid).strip()
        if kind not in {b'commit', b'tag'}:
            raise ValueError('Only commits and tags pointing to commits are supported')
        commit = git_output(git, environment, 'rev-parse', '--verify',
                            local_oid + '^{commit}').decode('ascii').strip()
        if not pattern.fullmatch(commit):
            raise ValueError('Invalid peeled commit')
        commits.add(commit)
    return sorted(commits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pre-push', action='store_true', help='read Git pre-push records from stdin')
    args = parser.parse_args()
    environment = dict(os.environ, GIT_CONFIG_COUNT='1',
                       GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(ROOT),
                       GIT_NO_REPLACE_OBJECTS='1')
    git = ['git', '-C', str(ROOT)]
    try:
        if git_output(git, environment, 'rev-parse', '--is-shallow-repository').strip() != b'false':
            raise ValueError('Full archive history is required')
        pending = pending_commits(sys.stdin, git, environment) if args.pre_push else []
        # Keep the full local-ref audit, adding outgoing objects even without local refs.
        # Do not subtract remote tips: they may be unavailable locally or omit history
        # that the archive policy still requires us to inspect.
        commits = git_output(git, environment, 'rev-list', '--all', *pending, '--').decode('ascii').splitlines()
        for commit in commits:
            names = git_output(git, environment, 'ls-tree', '-r', '--name-only', '-z',
                               commit).decode('utf-8', errors='surrogateescape').split('\0')
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
        # Both controls inspect this same frozen commit list. Batching bounds argv;
        # --no-walk prevents Gitleaks from changing the scope by following parents.
        for offset in range(0, len(commits), SCAN_BATCH_SIZE):
            log_opts = '--no-walk --diff-merges=first-parent ' + ' '.join(commits[offset:offset + SCAN_BATCH_SIZE]) + ' --'
            result = subprocess.run([executable, 'git', '--redact=100', '--no-banner',
                                     '--log-opts=' + log_opts, '--log-level=error', str(ROOT)],
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
