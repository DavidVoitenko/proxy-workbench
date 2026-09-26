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
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from verify_delivery import child_environment, stop as stop_process


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
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            value = None
        if value:
            return value
        time.sleep(step)
    raise TimeoutError('timed out')


def read_json(path):
    """Read a file that a background process may be replacing atomically."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def log_tail(path, limit=8000):
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def main(command):
    if not command:
        raise SystemExit('pass an installed proxy-workbench command or executable')
    command = [str(Path(shutil.which(command[0]) or command[0]).resolve()), *command[1:]]
    try:
        version = subprocess.run([*command, '--version'], capture_output=True, text=True, timeout=120)
    except OSError as exc:
        raise SystemExit(f'cannot run {command[0]!r}: {exc}') from exc
    print(version.stdout.strip() or version.stderr.strip())
    assert version.returncode == 0 and 'Proxy Workbench' in version.stdout + version.stderr
    mock = ThreadingHTTPServer(('127.0.0.1', 0), MockProxy)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    data = Path(tempfile.mkdtemp())
    env = child_environment(data / 'home', {'PROXY_WORKBENCH_DATA': str(data),
                                         'PYTHONUNBUFFERED': '1',
                                         'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
                                         'NO_PROXY': '127.0.0.1,localhost'})
    gui = None
    gui_log = None
    gui_log_path = data / 'smoke-desktop.log'
    client = None
    try:
        # An empty collect creates the database; then the mock proxy is added as a
        # candidate through the engine's own writer.  Writing the row with plain
        # SQL is not an option any more and must not become one again: after the
        # versioned schema the table carries a second column, so a positional
        # INSERT is exactly the "old binary writes into the new schema" failure
        # F24 requires (CONTRACTS §3.5.2).  A loopback address is only accepted
        # from a list the user handed over locally, hence the flag.
        listing = data / 'smoke-mock.txt'
        listing.write_text(f'http://127.0.0.1:{mock.server_port}\n', encoding='utf-8')
        subprocess.run([*command, 'collect', '--no-sources', '--data', str(data),
                        '--input', str(listing), '--allow-private-endpoints'],
                       check=True, timeout=120, capture_output=True, cwd=data, env=env)
        # Let the OS choose every port. Fixed CI ports made this smoke test fail
        # whenever another local test or a developer's service happened to use one.
        gui_log = gui_log_path.open('wb')
        gui = subprocess.Popen([*command, '--no-browser', '--no-tray', '--port', '0', '--data', str(data),
                                '--api-port', '0', '--gateway-port', '0'],
                               stdout=gui_log, stderr=subprocess.STDOUT, cwd=data, env=env)

        def process_check(callback, timeout=60):
            def checked():
                if gui.poll() is not None:
                    raise RuntimeError(f'GUI exited with code {gui.returncode}')
                return callback()
            return wait_for(checked, timeout=timeout)

        gui_port = process_check(lambda: (read_json(data / 'gui-address.json') or {}).get('port'))
        base = f'http://127.0.0.1:{gui_port}'
        page = process_check(lambda: httpx.get(base + '/', trust_env=False).text)
        token = re.search(r'workbench-token" content="([^"]+)"', page).group(1)
        client = httpx.Client(base_url=base, headers={'X-Workbench-Token': token}, trust_env=False, timeout=15)
        second = subprocess.run([*command, '--data', str(data), '--no-browser', '--no-tray'],
                                cwd=data, env=env, capture_output=True, text=True, timeout=30)
        assert second.returncode == 0, second.stdout + second.stderr
        assert 'already running' in second.stdout, 'a second start did not reach the desktop host'
        # A package can boot with the catalog or nested translations absent.
        # Exercise the served product, including resources older wheels lost.
        catalog = client.get('/api/source-catalog')
        catalog.raise_for_status()
        assert catalog.json().get('sources'), 'the installed source catalog is empty'
        for language in ('de', 'es', 'fr', 'it', 'ja', 'pl', 'pt', 'tr', 'uk', 'zh'):
            pack = client.get(f'/i18n/{language}.js')
            pack.raise_for_status()
            assert pack.text.strip(), f'the {language} translation is empty'
        settings = client.get('/api/defaults').json()
        settings.update(targets=[dict(name='mock', url='http://service.invalid/health', contains='healthy',
                                      statuses=[200], headers={}, method='GET')],
                        use_sources=False, workers=1, rate=0, timeout=5, min_success=1)
        client.post('/api/start', json=dict(action='scan', settings=settings)).raise_for_status()
        state = process_check(lambda: (s := client.get('/api/state').json())
                              and s.get('api') and s.get('gateway') and not s['running']
                              and s['job'].get('exit_code') is not None and s, timeout=120)
        print(json.dumps(dict(exit_code=state['job']['exit_code'], passed=state['export'].get('passed'))))
        assert state['job']['exit_code'] == 0, state['log']
        assert state['export']['passed'] == 1, state['log']
        proxy = httpx.get(f'{state["api"]}/random?format=txt', trust_env=False).text.strip()
        assert proxy == f'http://127.0.0.1:{mock.server_port}', proxy
        # The rotating proxy has its own password now, not the GUI session token
        # (defect 18 / F29): an unauthenticated request gets 407, so the client
        # uses the address the GUI itself hands to a phone.  The password is
        # minted per run and is never printed.
        #
        # The listener re-reads the published set on its own interval instead of
        # following every write, so a request sent in the same second as the
        # publication is answered by the *previous* generation.  Waiting for the
        # new one to be picked up is part of the scenario, not a retry of a
        # failure.
        gateway = state['gateway']['copy_address']

        def through_gateway():
            with httpx.Client(proxy=gateway, trust_env=False, timeout=15) as through:
                return through.get('http://service.invalid/through-gateway').text

        answer = ''
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            answer = through_gateway()
            if answer == 'healthy':
                break
            time.sleep(0.5)
        assert answer == 'healthy', answer
        # A schedule must reach the durable worker while the application is
        # awake; recording a requested slot alone used to look like success.
        client.post('/api/schedules/action', json={'action': 'add', 'id': 'package-smoke',
                                                   'interval_minutes': 60,
                                                   'budgets': {'requests': 5}}).raise_for_status()
        requested = client.post('/api/schedules/action', json={'action': 'run-now', 'id': 'package-smoke'})
        requested.raise_for_status()
        run_id = requested.json()['run_id']
        def schedule_finished():
            response = client.get('/api/schedules')
            response.raise_for_status()
            for schedule in response.json()['schedules']:
                for run in schedule['recent']:
                    if run['id'] == run_id and run['state'] not in ('requested', 'queued', 'running'):
                        return run
        scheduled = process_check(schedule_finished, timeout=60)
        assert scheduled['state'] == 'succeeded', scheduled
        print('smoke test passed')
    except Exception:
        if gui is not None:
            print('Desktop child output:\n' + (log_tail(gui_log_path) or '<empty>'), file=sys.stderr)
            print('Desktop journal:\n' + (log_tail(data / 'desktop-journal.jsonl') or '<not created>'),
                  file=sys.stderr)
        raise
    finally:
        if client is not None:
            client.close()
        if gui is not None:
            stop_process(gui, timeout=10)
        if gui_log is not None:
            gui_log.close()
        mock.shutdown()
        mock.server_close()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1:] or ['proxy-workbench'])
