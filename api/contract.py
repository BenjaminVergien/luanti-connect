"""Strict network validation; the mod remains authoritative for account state."""
import re
from tools import world_worker

ACCOUNT_MAX = 256 * 1024
LIFECYCLE_MAX = world_worker.MAX_BYTES
JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
# Luanti's documented player-name character set and 20-byte limit.
USERNAME = re.compile(r'[A-Za-z0-9_-]{1,20}\Z')


def validate_accounts(raw):
    if len(raw) > ACCOUNT_MAX:
        raise ValueError('Request too large')
    value = world_worker.decode(raw)
    if not isinstance(value, dict) or set(value) != {'version', 'job_id', 'operation', 'accounts'}:
        raise ValueError('Invalid account envelope')
    if type(value['version']) is not int or value['version'] != 1 or value['operation'] != 'sync_accounts':
        raise ValueError('Unsupported contract')
    if not isinstance(value['job_id'], str) or not JOB_ID.fullmatch(value['job_id']):
        raise ValueError('Invalid job ID')
    accounts = value['accounts']
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 100:
        raise ValueError('Invalid account count')
    seen = set()
    for account in accounts:
        required = {'username', 'role', 'admitted'}
        if (not isinstance(account, dict) or not required <= set(account)
                or set(account) - required - {'initial_password'}):
            raise ValueError('Invalid account fields')
        name = account['username']
        if not isinstance(name, str) or not USERNAME.fullmatch(name) or name.lower() in seen:
            raise ValueError('Invalid or duplicate username')
        seen.add(name.lower())
        if account['role'] not in ('participant', 'facilitator') or type(account['admitted']) is not bool:
            raise ValueError('Invalid role or admission')
        if 'initial_password' in account and (not isinstance(account['initial_password'], str) or not account['initial_password']):
            raise ValueError('Invalid initial password')
    return value


def validate_lifecycle(raw):
    return world_worker.validate(raw)[0]
