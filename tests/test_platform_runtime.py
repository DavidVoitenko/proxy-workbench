"""Native locks, launchers and path isolation, with temporary data only."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))

from proxy_workbench import __main__ as entry, desktop
import verify_delivery


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pw-platform-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.layout = desktop.resolve_layout({desktop.DATA_ENV: str(self.root / 'Данные 路径')})

    def test_record_remains_readable_while_another_process_holds_lock(self):
        owner = desktop.InstanceLock(self.layout)
        self.addCleanup(owner.release)
        self.assertTrue(owner.acquire())
        record = desktop.Instance(os.getpid(), 'tcp://127.0.0.1:12345', 'local-test-token')
        self.assertEqual(owner.publish(record), record)
        # Windows byte locks are mandatory for reads as well as writes. This
        # separate process must see the JSON even though it cannot own the lock.
        child = '''
import json, sys
from proxy_workbench import desktop
layout = desktop.resolve_layout({desktop.DATA_ENV: sys.argv[1]})
record = desktop.read_instance(layout)
lock = desktop.InstanceLock(layout)
owned = lock.acquire()
print(json.dumps({'owned': owned, 'record': record.as_dict() if record else None}))
lock.release()
'''
        def run_child():
            result = subprocess.run([sys.executable, '-c', child, str(self.layout.data)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=20,
                                    env=dict(os.environ, PYTHONUTF8='1'))
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        result = run_child()
        self.assertFalse(result['owned'])
        self.assertEqual(result['record'], record.as_dict())
        self.assertLess(owner.path.stat().st_size, 4096)
        owner.release()
        result = run_child()
        self.assertTrue(result['owned'])
        self.assertIsNone(result['record'])

    @unittest.skipIf(os.name == 'nt', 'Windows uses the authenticated TCP control channel')
    def test_control_channel_works_with_long_unicode_data_path_and_cleans_up(self):
        path = self.root / ('данные-' * 15) / 'workspace'
        layout = desktop.resolve_layout({desktop.DATA_ENV: str(path)})
        server = desktop.ControlServer(layout, lambda request: {'ok': True, 'echo': request['action']})
        self.addCleanup(server.stop)
        address = server.start()
        self.assertTrue(address, 'a long data path must still have a working control channel')
        self.assertLessEqual(len(os.fsencode(server.socket_path)), 100)
        self.assertEqual(desktop.control_request(address, server.token, {'action': 'hello'}),
                         {'ok': True, 'echo': 'hello'})
        self.assertFalse(desktop.control_request(address, 'wrong-token', {'action': 'hello'})['ok'])
        socket_dir = server.socket_path.parent
        self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
        server.stop()
        self.assertFalse(socket_dir.exists())

    def test_explicit_data_reaches_host_lock_and_gui_before_launch(self):
        from proxy_workbench import gui
        target = self.root / 'custom data'
        host = mock.MagicMock()
        host.claim.return_value = True
        host.defer_to_interface.return_value = -1
        host.argv = ['--no-browser']
        host.background = False
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(desktop, 'DesktopHost', return_value=host) as constructor, \
                mock.patch.object(gui, 'main', return_value=0) as gui_main:
            self.assertEqual(desktop.main(['--data', str(target), '--no-browser']), 0)
            layout = constructor.call_args.args[0]
            self.assertEqual(layout.data, target)
            self.assertEqual(layout.cache, target / 'cache')
            self.assertEqual(layout.logs, target / 'logs')
            gui_main.assert_called_once_with(['--data', str(target), '--no-browser'])
        host.release.assert_called_once()

    def test_desktop_gui_capture_preserves_background_executor_option(self):
        from proxy_workbench import gui
        host = desktop.DesktopHost(self.layout, tray=False)
        with mock.patch.object(gui, 'make_server') as create, mock.patch.object(host, 'adopt') as adopt:
            with host.gui_handle():
                server = gui.make_server(self.layout.data, 0, execute_jobs=True)
            create.assert_called_once_with(self.layout.data, 0, execute_jobs=True)
            adopt.assert_called_once_with(server)
            self.assertIs(gui.make_server, create)

    def test_custom_data_does_not_import_another_workspaces_private_files(self):
        checkout = self.root / 'checkout'
        legacy = checkout / 'data'
        legacy.mkdir(parents=True)
        (legacy / 'private-settings.json').write_text('{"private": true}', encoding='utf-8')
        report = desktop.run_startup_migration(self.layout, environ={},
                                              package=checkout / 'proxy_workbench')
        self.assertFalse(report['applied'])
        self.assertFalse(self.layout.data.exists())
        self.assertEqual(list(checkout.iterdir()), [legacy])

    def test_data_before_status_reads_only_the_selected_workspace(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            result = entry.main(['--data', str(self.layout.data), '--status'])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {'running': False, 'data': str(self.layout.data)})
        self.assertFalse(self.layout.data.exists())

    def test_no_browser_is_preserved_on_second_launch(self):
        host = desktop.DesktopHost(self.layout, ['--no-browser'], tray=False)
        record = desktop.Instance(os.getpid(), 'tcp://127.0.0.1:12345', 'local-test-token')
        with mock.patch.object(host, 'running_instance', return_value=record), \
                mock.patch.object(desktop, 'control_request', return_value={'ok': True}) as request, \
                mock.patch.object(desktop, 'journal'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(host.forward(), 0)
        self.assertFalse(request.call_args.args[2]['open_browser'])
        with mock.patch.object(host, 'status', return_value={'ok': True}), \
                mock.patch.object(host, 'open_interface') as open_page:
            self.assertTrue(host.handle({'action': 'activate', 'open_browser': False})['ok'])
        open_page.assert_not_called()

    def test_router_does_not_confuse_option_values_with_verbs(self):
        for argv, expected in [
            (['--data', 'scan'], 'desktop'),
            (['--data=scan'], 'desktop'),
            (['--gateway-host', 'gateway'], 'desktop'),
            (['--gateway-interface', 'scan'], 'desktop'),
            (['--data', 'scan', 'export'], 'cli'),
            (['--data', 'folder', 'sources', 'list'], 'cli'),
            (['--workers', '8', 'scan'], 'cli'),
            (['gui', '--data', 'scan'], 'interface'),
            (['--no-desktop', '--data', 'scan'], 'interface'),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(entry.resolve(argv)[0], expected)

    def test_delivery_environment_removes_private_overrides_and_agrees_on_paths(self):
        private = {key: 'must-not-inherit' for key in (
            desktop.DATA_ENV, desktop.CACHE_ENV, desktop.LOGS_ENV, desktop.PORTABLE_ENV,
            desktop.UPDATE_MANIFEST_ENV, 'PROXY_WORKBENCH_PREVIOUS_DATA')}
        with mock.patch.dict(os.environ, private):
            env = verify_delivery.child_environment(self.root)
        self.assertTrue(all(key not in env for key in private))
        resolved = desktop.resolve_layout(env, frozen_=True)
        expected = verify_delivery.per_user_folders(self.root, env)
        for key in ('data', 'cache', 'logs'):
            self.assertEqual(expected[key], getattr(resolved, key))

    @unittest.skipIf(os.name == 'nt', 'POSIX source launchers')
    def test_shell_launchers_preserve_arguments_and_skip_installed_dependencies(self):
        # No pip or system Python is invoked. The existing venv stand-in records
        # the calls, so the test also catches bash-only command substitutions.
        project = self.root / 'source with spaces'
        python = project / '.venv' / 'bin' / 'python'
        python.parent.mkdir(parents=True)
        requirements = b'local-fixture-only\n'
        (project / 'requirements.txt').write_bytes(requirements)
        digest = hashlib.sha256(requirements).hexdigest()
        (project / '.venv' / '.dependencies-ready').write_text(digest + '\n')
        for name in ('run.sh', 'Start.command'):
            shutil.copy2(ROOT / name, project / name)
        python.write_text('#!/bin/sh\n'
                          'case "$1" in\n'
                          '  -c) case "$2" in *hashlib*) cat .venv/.dependencies-ready;; esac; exit 0;;\n'
                          'esac\n'
                          'printf "%s\\n" "$@"\n', encoding='utf-8')
        python.chmod(0o755)
        for name in ('run.sh', 'Start.command'):
            result = subprocess.run(['/bin/sh', str(project / name), '--data', 'папка с пробелами'],
                                    cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['-m', 'proxy_workbench', '--data', 'папка с пробелами'])

    @unittest.skipUnless(os.name == 'nt', 'native Windows batch launcher')
    def test_batch_launcher_uses_existing_venv_and_preserves_arguments(self):
        project = self.root / 'source with spaces'
        project.mkdir()
        shutil.copy2(ROOT / 'Start.bat', project / 'Start.bat')
        requirements = b'# local fixture only\n'
        (project / 'requirements.txt').write_bytes(requirements)
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(project / '.venv')],
                       check=True, capture_output=True, timeout=45)
        (project / '.venv' / '.dependencies-ready').write_text(hashlib.sha256(requirements).hexdigest() + '\n')
        module = project / 'proxy_workbench'
        module.mkdir()
        (module / '__init__.py').write_text('', encoding='utf-8')
        (module / '__main__.py').write_text('import json, sys; print(json.dumps(sys.argv[1:]))', encoding='utf-8')
        # The venv deliberately has no pip: a broken dependency-cache comparison
        # fails, rather than installing anything from the network.
        result = subprocess.run(['cmd', '/d', '/c', 'Start.bat', '--data', 'папка с пробелами'],
                                cwd=project, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), ['--data', 'папка с пробелами'])


class ScheduleBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pw-schedule-host-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.layout = desktop.resolve_layout({desktop.DATA_ENV: str(self.root)})
        self.host = desktop.DesktopHost(self.layout, tray=False)
        self.submitted = []
        module = types.ModuleType('proxy_workbench.jobrunner')
        def submit(data, run, *, db_path=None):
            self.assertEqual(Path(data), self.root)
            self.assertEqual(db_path, self.root / 'proxies.sqlite3')
            self.submitted.append(run)
            return types.SimpleNamespace(id='job-' + run.run_id, state='queued')
        module.submit_schedule = submit
        patch = mock.patch.dict(sys.modules, {'proxy_workbench.jobrunner': module})
        patch.start()
        self.addCleanup(patch.stop)

    def add_schedule(self, elapsed=61):
        from proxy_workbench import scheduler
        connection, engine = desktop.open_schedule_engine(self.layout)
        self.addCleanup(connection.close)
        engine.add(scheduler.ScheduleSpec(id='periodic', interval_minutes=1, action='check',
                                          collection_id='public', budgets=scheduler.Budgets(requests=7)))
        state = engine.state('periodic')
        state.activated_at = time.time() - elapsed
        engine.store.save_state('periodic', state)
        return connection, engine

    def test_due_run_becomes_a_job_once_and_preserves_scope_budget(self):
        connection, engine = self.add_schedule()
        report = self.host.schedule_tick()
        self.assertEqual(len(report['submitted']), 1)
        self.assertEqual(report['failed'], [])
        request = self.submitted[0]
        self.assertEqual(request.action, 'check')
        self.assertEqual(request.collection_id, 'public')
        self.assertEqual(request.budgets.requests, 7)
        state, encoded = connection.execute(
            'SELECT state, counters_json FROM schedule_run WHERE id=?', (request.run_id,)).fetchone()
        self.assertEqual(state, 'queued')
        self.assertEqual(json.loads(encoded)['job_id'], report['submitted'][0])
        self.assertEqual(self.host.schedule_tick()['submitted'], [])
        self.assertEqual(len(self.submitted), 1)

    def test_manual_requested_run_waits_for_external_pause_then_dispatches(self):
        connection, engine = self.add_schedule(elapsed=0)
        request = engine.run_now('periodic')
        engine.pause('periodic')
        self.assertEqual(self.host.schedule_tick()['submitted'], [])
        self.assertEqual(connection.execute('SELECT state FROM schedule_run WHERE id=?',
                                            (request.run_id,)).fetchone()[0], 'requested')
        engine.resume('periodic')
        self.assertEqual(len(self.host.schedule_tick()['submitted']), 1)
        self.assertEqual(self.submitted[0].run_id, request.run_id)

    def test_wake_coalesces_missed_intervals_before_job_submission(self):
        self.add_schedule(elapsed=601)
        report = self.host.schedule_tick(woke=True)
        self.assertTrue(report['woke'])
        self.assertEqual(len(self.submitted), 1)
        self.assertTrue(self.submitted[0].coalesced)
        self.assertGreater(self.submitted[0].missed, 1)

    def test_busy_writer_preserves_wake_until_shared_timer_can_tick(self):
        from proxy_workbench.maintenance import exclusive_lock
        self.add_schedule(elapsed=601)
        with exclusive_lock(self.root / 'workbench.lock'):
            report = self.host.schedule_tick(woke=True)
        self.assertTrue(report['busy'])
        self.assertEqual(self.submitted, [])
        report = self.host.schedule_tick()
        self.assertTrue(report['woke'])
        self.assertEqual(len(self.submitted), 1)
        self.assertTrue(self.submitted[0].coalesced)


class ScheduleExecutionTests(unittest.TestCase):
    def test_persisted_schedule_reaches_shared_executor_and_records_spend(self):
        from proxy_workbench import jobrunner, scheduler
        with tempfile.TemporaryDirectory(prefix='pw-scheduled-execution-') as directory:
            data = Path(directory).resolve()
            layout = desktop.resolve_layout({desktop.DATA_ENV: str(data)})
            connection, engine = desktop.open_schedule_engine(layout)
            try:
                engine.add(scheduler.ScheduleSpec(id='local-check', interval_minutes=1,
                                                  budgets=scheduler.Budgets(requests=7)))
                state = engine.state('local-check')
                state.activated_at = time.time() - 61
                engine.store.save_state('local-check', state)
                calls = []
                async def local_scan(connection, config, **kwargs):
                    calls.append(kwargs['job_id'])
                    self.assertEqual(kwargs['max_requests'], 7)
                    kwargs['run_state'].update(state='complete', requests=2, bytes=10)
                host = desktop.DesktopHost(layout, tray=False)
                submitted = host.schedule_tick()['submitted']
                self.assertEqual(len(submitted), 1)
                runner = jobrunner.JobRunner(data, scan=local_scan)
                result = runner.run_pending()
                self.assertEqual([item['state'] for item in result], ['succeeded'], result)
                self.assertEqual(calls, submitted)
                self.assertEqual(runner.run_pending(), [])
                state, encoded = connection.execute(
                    'SELECT state, counters_json FROM schedule_run WHERE schedule_id=?', ('local-check',)).fetchone()
                self.assertEqual(state, 'succeeded')
                self.assertEqual(json.loads(encoded)['usage']['requests'], 2)
                self.assertEqual(engine.store.load_state('local-check').counters.requests, 2)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
