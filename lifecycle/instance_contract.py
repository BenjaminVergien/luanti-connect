"""Strict instance operations shared by HTTPS, worker, and privileged controller."""
import re
import json

ID = re.compile(r'g[0-9a-z]{26}\Z')
OPERATIONS = ('create_instance', 'configure_instance', 'start_instance', 'stop_instance', 'instance_status', 'archive_instance')


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate request field')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def validate(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 2
            or not isinstance(value.get('job_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['job_id'])
            or not isinstance(value.get('instance_id'), str) or not ID.fullmatch(value['instance_id'])
            or value.get('operation') not in OPERATIONS):
        raise ValueError('Invalid instance request')
    fields = {'version', 'job_id', 'instance_id', 'operation'}
    if value['operation'] == 'create_instance':
        fields |= {'template_id', 'template_revision', 'access_window'}
        if (not isinstance(value.get('template_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value['template_id'])
                or not isinstance(value.get('template_revision'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['template_revision'])):
            raise ValueError('Invalid template reference')
    if value['operation'] == 'configure_instance':
        fields |= {'access_window', 'generation', 'enabled'}
        if type(value.get('generation')) is not int or not 1 <= value['generation'] <= 2**53 - 1 or type(value.get('enabled')) is not bool:
            raise ValueError('Invalid instance generation')
    if 'access_window' in fields:
        window = value.get('access_window')
        if (not isinstance(window, dict) or set(window) != {'starts_at', 'ends_at'}
                or any(type(window[key]) is not int or abs(window[key]) > 2**53 - 1 for key in window)
                or window['starts_at'] >= window['ends_at']):
            raise ValueError('Invalid approved window')
    if set(value) != fields:
        raise ValueError('Unexpected instance fields')
    return value
