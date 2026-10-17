#!/usr/bin/env python3
"""Portable packaging, contract and installer validation checks."""
import hashlib
import io
from http.client import HTTPMessage
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.development import allowed, bundle, extract, source_files
from lifecycle.install_docker import configuration
from lifecycle.errors import LifecycleError
from api.development_proxy import Proxy, LIMIT


class DevelopmentTests(unittest.TestCase):
    def test_development_proxy_preserves_large_account_request_bytes_and_rejects_excess(self):
        for size in (20000, LIMIT, LIMIT + 1):
            with self.subTest(size=size):
                request = object.__new__(Proxy)
                request.headers = HTTPMessage()
                request.headers['Host'] = 'localhost:18443'
                request.headers['Authorization'] = 'Bearer ' + 'x' * 43
                request.headers['Content-Length'] = str(size)
                request.path = '/v1/worlds/g00000000000000000000000000/accounts/jobs'
                request.command = 'POST'
                request.connection = Mock()
                raw = b'{' + b' ' * (size - 2) + b'}'
                request.rfile, request.wfile = io.BytesIO(raw), io.BytesIO()
                for name in ('send_error', 'send_response', 'send_header', 'end_headers'):
                    setattr(request, name, Mock())
                upstream = Mock()
                upstream.getresponse.return_value.status = 202
                upstream.getresponse.return_value.read.return_value = b'{}'
                upstream.getresponse.return_value.getheader.side_effect = {'Cache-Control': 'no-store', 'Location': '/v1/lifecycle/jobs/example'}.get
                with patch('api.development_proxy.http.client.HTTPConnection', return_value=upstream) as connection:
                    request.forward()
                if size > LIMIT:
                    request.send_error.assert_called_once_with(413)
                    connection.assert_not_called()
                else:
                    request.send_error.assert_not_called()
                    self.assertEqual(raw, upstream.request.call_args.kwargs['body'])
                    request.send_response.assert_called_once_with(202)
                    request.send_header.assert_any_call('Cache-Control', 'no-store')
                    request.send_header.assert_any_call('Location', '/v1/lifecycle/jobs/example')
                    self.assertEqual(b'{}', request.wfile.getvalue())

    def test_credentials_instructions_and_runtime_are_never_packaged(self):
        for name in ('.git/config', 'AGENTS.md', '.agents/rules.py', 'api/.env', 'api/secret.key', 'world/auth.sqlite',
                     'tests/__pycache__/x.py', 'tools/../api/app.py', '/api/app.py', 'api\\app.py', 'api/config.json',
                     'examples/secrets/token.json', 'examples/token.json', 'examples/.private.json', 'examples/receipts/job.json'):
            self.assertFalse(allowed(name), name)
        self.assertTrue(allowed('api/app.py'))
        self.assertTrue(allowed('lifecycle/docker_backend.py'))
        self.assertGreater(len(source_files()), 30)

    def test_bundle_is_repeatable_and_extraction_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / 'first.zip', base / 'second.zip'
            receipt = bundle(first)
            self.assertEqual(receipt, bundle(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            manifest = extract(first, base / 'installed', receipt['archive_sha256'])
            self.assertEqual(receipt['sha256'], manifest['sha256'])
            with self.assertRaises(ValueError):
                extract(first, base / 'bad', '0' * 64)
            with zipfile.ZipFile(second, 'a') as archive:
                archive.writestr('../outside.py', 'unsafe')
            with self.assertRaises(ValueError):
                extract(second, base / 'bad', hashlib.sha256(second.read_bytes()).hexdigest())
            self.assertFalse((base / 'outside.py').exists())

    def test_installer_requires_explicit_content_and_supports_public_hostname_on_windows(self):
        settings = {'image': 'ghcr.io/luanti-org/luanti:5.17.0', 'game_source': '/reviewed/game',
                    'admin_name': 'Administrator', 'hostname': 'api.example.org'}
        controller, api = configuration(settings, 'sha256:' + 'a' * 64, '11111111-1111-4111-8111-111111111111')
        self.assertEqual({}, controller['worlds'])
        self.assertEqual({}, api['templates'])
        self.assertEqual('api.example.org', api['hostname'])
        for changes in ({'hostname': 'localhost'}, {'uid': 0}, {'image': 'untrusted/image:latest'}, {'docker_args': '--privileged'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, LifecycleError)):
                configuration(dict(settings, **changes), 'sha256:' + 'a' * 64, '11111111-1111-4111-8111-111111111111')


if __name__ == '__main__':
    unittest.main(verbosity=2)
