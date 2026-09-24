"""End-to-end smoke test of a built package or executable, using only local mock services.

    python packaging/smoke.py proxy-workbench          # installed with pip/pipx
    python packaging/smoke.py dist/proxy-workbench.exe

It starts the GUI, runs a check through a local mock proxy, and reads the result
back through the API and the rotating gateway. No public network is touched.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


class MockProxy(BaseHTTPRequestHandler):
    """Answers every proxied request like a healthy service would."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'healthy'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_for(check, timeout=60, step=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except (httpx.HTTPError, OSError):
            value = None
        if value:
            return value
        time.sleep(step)
    raise TimeoutError('timed out')


def main(command):
    command = [shutil.which(command[0]) or command[0], *command[1:]]
    version = subprocess.run([*command, '--version'], capture_output=True, text=True, timeout=120)
    print(version.stdout.strip() or version.stderr.strip())
    assert version.returncode == 0 and 'Proxy Workbench' in version.stdout + version.stderr
    mock = ThreadingHTTPServer(('127.0.0.1', 0), MockProxy)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    data = Path(tempfile.mkdtemp())
    # An empty collect creates the database; then the mock proxy is added as a candidate.
    subprocess.run([*command, 'collect', '--no-sources', '--data', str(data)], check=True, timeout=120,
                   capture_output=True)
    with sqlite3.connect(data / 'proxies.sqlite3') as db:
        db.execute('INSERT INTO candidates VALUES (?)', (f'http://127.0.0.1:{mock.server_port}',))
    ports = dict(gui=18731, api=18732, gateway=18733)
    gui = subprocess.Popen([*command, 'gui', '--no-browser', '--port', str(ports['gui']), '--data', str(data),
                            '--api-port', str(ports['api']), '--gateway-port', str(ports['gateway'])],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f'http://127.0.0.1:{ports["gui"]}'
        page = wait_for(lambda: httpx.get(base + '/', trust_env=False).text)
        token = re.search(r'workbench-token" content="([^"]+)"', page).group(1)
        client = httpx.Client(base_url=base, headers={'X-Workbench-Token': token}, trust_env=False, timeout=15)
        settings = client.get('/api/defaults').json()
        settings.update(targets=[dict(name='mock', url='http://service.invalid/health', contains='healthy',
                                      statuses=[200], headers={}, method='GET')],
                        use_sources=False, workers=1, rate=0, timeout=5, min_success=1)
        client.post('/api/start', json=dict(action='scan', settings=settings)).raise_for_status()
        state = wait_for(lambda: (s := client.get('/api/state').json()) and not s['running']
                         and s['job'].get('exit_code') is not None and s, timeout=120)
        print(json.dumps(dict(exit_code=state['job']['exit_code'], passed=state['export'].get('passed'))))
        assert state['job']['exit_code'] == 0, state['log']
        assert state['export']['passed'] == 1, state['log']
        proxy = httpx.get(f'http://127.0.0.1:{ports["api"]}/random?format=txt', trust_env=False).text.strip()
        assert proxy == f'http://127.0.0.1:{mock.server_port}', proxy
        with httpx.Client(proxy=f'http://127.0.0.1:{ports["gateway"]}', trust_env=False, timeout=15) as through:
            assert through.get('http://service.invalid/through-gateway').text == 'healthy'
        print('smoke test passed')
    finally:
        gui.terminate()
        try:
            gui.wait(10)
        except subprocess.TimeoutExpired:
            gui.kill()
        mock.shutdown()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1:] or ['proxy-workbench'])
