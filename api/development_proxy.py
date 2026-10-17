#!/usr/bin/env python3
"""Loopback-only HTTPS proxy for the private development SSH tunnel."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl

TLS = Path('/etc/civicrm-bridge-api/development-tls')
LIMIT = 256 * 1024


class Proxy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def forward(self):
        self.connection.settimeout(620)
        if (self.headers.get_all('Host') not in (['localhost:18443'], ['localhost:8443'])
                or self.headers.get('Transfer-Encoding') is not None
                or not self.path.startswith('/v1/') or '#' in self.path
                or len(self.headers.get_all('Authorization', [])) != 1):
            self.send_error(400)
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > LIMIT)):
            self.send_error(413)
            return
        body = self.rfile.read(int(lengths[0])) if lengths else None
        upstream = http.client.HTTPConnection('127.0.0.1', 8765, timeout=610)
        try:
            upstream.request(self.command, self.path, body=body,
                             headers={'Authorization': self.headers['Authorization'], 'Content-Type': 'application/json'})
            result = upstream.getresponse()
            data = result.read(LIMIT + 1)
            if len(data) > LIMIT:
                self.send_error(502)
                return
            self.send_response(result.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for name in ('Cache-Control', 'WWW-Authenticate', 'Location'):
                value = result.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            self.send_error(502)
        finally:
            self.close_connection = True
            upstream.close()

    do_GET = forward
    do_POST = forward


def main():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(TLS / 'server.crt', TLS / 'server.key')
    server = ThreadingHTTPServer(('127.0.0.1', 8443), Proxy)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
