"""Where a run writes, and what a packaged build is allowed to write.

F23 requires per-user data/cache/logs, an explicit portable mode, correct
resources, cwd and worker command, and Unicode paths on a read-only bundle.  The
rule order is a contract, so the tests below state it as one.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import desktop


def temp(*parts):
    return Path(tempfile.mkdtemp(prefix='pw-layout-')).resolve().joinpath(*parts)


class ResolutionOrderTests(unittest.TestCase):
    """One table, one order: environment, portable, installed, checkout, per-user."""

    def layout(self, environ=None, **kwargs):
        kwargs.setdefault('frozen_', False)
        kwargs.setdefault('package', Path(__file__).resolve().parents[1] / 'proxy_workbench')
        return desktop.resolve_layout(environ=environ if environ is not None else {}, **kwargs)

    def test_explicit_data_path_wins_over_everything_else(self):
        chosen = temp('Данные прокси', 'unicode-路径')
        result = self.layout({desktop.DATA_ENV: str(chosen)}, platform='darwin', home=temp('home'))
        self.assertEqual(result.data, chosen)
        self.assertEqual(result.mode, 'environment')

    def test_explicit_data_path_gets_cache_and_logs_inside_it(self):
        chosen = temp('data-únïcode')
        result = self.layout({desktop.DATA_ENV: str(chosen)}, platform='linux', home=temp('home'))
        self.assertEqual(result.cache, chosen / 'cache')
        self.assertEqual(result.logs, chosen / 'logs')

    def test_cache_and_logs_can_be_pointed_separately(self):
        chosen = temp('data')
        result = self.layout({desktop.DATA_ENV: str(chosen),
                              desktop.CACHE_ENV: str(temp('cache')), desktop.LOGS_ENV: str(temp('logs'))})
        self.assertNotEqual(result.cache, result.data / 'cache')
        self.assertNotEqual(result.logs, result.data / 'logs')

    def test_portable_environment_flag_keeps_everything_next_to_the_program(self):
        program = temp('Programm', 'portable-режим').resolve()
        program.mkdir(parents=True)
        result = self.layout({desktop.PORTABLE_ENV: '1'}, executable=str(program / 'app'),
                             platform='darwin', home=temp('home'))
        self.assertEqual(result.mode, 'portable')
        self.assertEqual(result.data, program / 'data')
        self.assertEqual(result.cache, program / 'cache')
        self.assertEqual(result.logs, program / 'logs')
        self.assertTrue(result.portable)

    def test_marker_file_makes_portable_mode_durable(self):
        program = temp('Program')
        program.mkdir(parents=True)
        desktop.write_portable_marker(program, environ={})
        self.assertTrue(desktop.is_portable(program))
        result = self.layout({}, executable=str(program / 'app'), platform='darwin', home=temp('home'))
        self.assertEqual(result.mode, 'portable')

    def test_portable_is_never_a_silent_fallback_for_a_writable_folder(self):
        # A writable program folder alone must not switch a user into portable
        # mode: that decision has to be explicit, or two windows could disagree
        # about where the data lives.
        program = temp('Program')
        program.mkdir(parents=True)
        result = self.layout({}, executable=str(program / 'app'), platform='darwin', home=temp('home'))
        self.assertNotEqual(result.mode, 'portable')

    def test_frozen_build_does_not_write_next_to_its_executable(self):
        # R19: frozen used to return <executable dir>/data, which cannot work in
        # Program Files or in a .app bundle.
        program = temp('Program Files', 'app')
        program.mkdir(parents=True)
        home = temp('home')
        result = self.layout({}, frozen_=True, executable=str(program / 'app.exe'),
                             platform='win32', home=home)
        self.assertEqual(result.mode, 'per-user')
        self.assertNotEqual(result.data, program / 'data')

    def test_read_only_program_folder_also_falls_back_to_per_user(self):
        program = temp('Locked')
        program.mkdir(parents=True)
        program.chmod(0o500)
        try:
            if os.access(program, os.W_OK):
                self.skipTest('this user can write to a 0o500 folder; the fallback cannot be observed')
            result = self.layout({}, frozen_=False, executable=str(program / 'app'),
                                 package=program / 'proxy_workbench', home=temp('home'))
            self.assertEqual(result.mode, 'per-user')
        finally:
            program.chmod(0o700)

    def test_source_checkout_keeps_the_data_folder_it_has_always_used(self):
        # The browser and headless paths must not change: Start.bat, run.sh and
        # the wheel all keep data/ next to the project.
        package = temp('checkout', 'proxy_workbench')
        package.parent.mkdir(parents=True)
        (package.parent / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')
        (package.parent / 'Start.bat').write_text('@echo off\n', encoding='utf-8')
        result = self.layout({}, package=package, platform='darwin', home=temp('home'))
        self.assertEqual(result.mode, 'checkout')
        self.assertEqual(result.data, package.parent / 'data')


class PerUserBaseTests(unittest.TestCase):
    """The three standard roots, so a user can predict where files go."""

    def layout(self, platform, environ=None):
        return desktop.resolve_layout(environ=environ or {}, frozen_=True, platform=platform,
                                      home=Path('/home/tester'), executable='/opt/app/app',
                                      package=Path('/opt/app/proxy_workbench'))

    def test_macos_uses_application_support_caches_and_logs(self):
        result = self.layout('darwin')
        home = Path('/home/tester')
        self.assertEqual(result.data, home / 'Library' / 'Application Support' / 'proxy-workbench')
        self.assertEqual(result.cache, home / 'Library' / 'Caches' / 'proxy-workbench')
        self.assertEqual(result.logs, home / 'Library' / 'Logs' / 'proxy-workbench')

    def test_windows_uses_local_app_data(self):
        result = self.layout('win32', {'LOCALAPPDATA': 'C:/Users/t/AppData/Local'})
        self.assertEqual(result.data, Path('C:/Users/t/AppData/Local/proxy-workbench'))

    def test_linux_follows_xdg_variables(self):
        result = self.layout('linux', {'XDG_DATA_HOME': '/xdg/data', 'XDG_CACHE_HOME': '/xdg/cache',
                                       'XDG_STATE_HOME': '/xdg/state'})
        self.assertEqual(result.data, Path('/xdg/data/proxy-workbench'))
        self.assertEqual(result.cache, Path('/xdg/cache/proxy-workbench'))
        self.assertEqual(result.logs, Path('/xdg/state/proxy-workbench/logs'))


class WritableFolderTests(unittest.TestCase):
    def test_unicode_paths_are_used_as_given(self):
        root = temp('Папка', 'プロキシ', 'ünïcode')
        layout = desktop.Layout(root, root / 'cache', root / 'logs', 'portable', root, 'test')
        desktop.ensure_layout(layout)
        for folder in (layout.data, layout.cache, layout.logs):
            self.assertTrue(folder.is_dir())
            self.assertTrue(os.access(folder, os.W_OK))

    def test_a_folder_that_cannot_be_created_names_itself_and_the_fix(self):
        with mock.patch.object(Path, 'mkdir', side_effect=OSError('read-only file system')):
            with self.assertRaises(desktop.LayoutError) as caught:
                desktop.ensure_layout(desktop.Layout(Path('/nope'), Path('/nope'), Path('/nope'),
                                                    'per-user', Path('/nope'), 'test'))
        message = str(caught.exception)
        self.assertIn('/nope', message)
        self.assertIn(desktop.DATA_ENV, message)


class WorkerCommandTests(unittest.TestCase):
    """A frozen build has no module to import, so the command must be the binary."""

    def test_frozen_worker_is_the_executable_itself(self):
        with mock.patch.object(desktop, 'frozen', return_value=True):
            command = desktop.worker_command('scan', '--data', '/tmp/d', executable='/opt/app/app')
        self.assertEqual(command, ['/opt/app/app', 'scan', '--data', '/tmp/d'])

    def test_source_worker_uses_the_module_entry_point(self):
        with mock.patch.object(desktop, 'frozen', return_value=False):
            command = desktop.worker_command('scan', executable='/usr/bin/python3')
        self.assertEqual(command, ['/usr/bin/python3', '-u', '-m', 'proxy_workbench', 'scan'])

    def test_child_environment_points_the_worker_at_the_resolved_folders(self):
        layout = desktop.Layout(Path('/d'), Path('/c'), Path('/l'), 'per-user', Path('/d'), 'test')
        env = desktop.child_environment(layout, base={'PATH': '/bin'})
        self.assertEqual(env[desktop.DATA_ENV], '/d')
        self.assertEqual(env[desktop.CACHE_ENV], '/c')
        self.assertEqual(env[desktop.LOGS_ENV], '/l')
        self.assertEqual(env['PYTHONUTF8'], '1')
        self.assertEqual(env['PATH'], '/bin')

    def test_child_environment_leaves_the_language_binding_to_its_owner(self):
        # gui.CHILD_ENV sets PROXY_WORKBENCH_LANG and the integration contract
        # makes that binding part of the interface contract; this module must
        # not set or unset it behind that owner's back.
        layout = desktop.Layout(Path('/d'), Path('/c'), Path('/l'), 'per-user', Path('/d'), 'test')
        env = desktop.child_environment(layout, base={'PROXY_WORKBENCH_LANG': 'ru'})
        self.assertEqual(env['PROXY_WORKBENCH_LANG'], 'ru')

    def test_worker_working_directory_is_writable_not_the_bundle(self):
        # gui.py starts workers with cwd=ROOT.parent, which inside a bundle is a
        # read-only unpack directory.
        layout = desktop.Layout(Path('/d'), Path('/c'), Path('/l'), 'per-user', Path('/d'), 'test')
        self.assertEqual(desktop.child_cwd(layout), Path('/d'))


class ResourceTests(unittest.TestCase):
    def test_packed_resources_come_from_the_unpack_directory(self):
        with mock.patch.object(sys, '_MEIPASS', '/tmp/_MEI12345', create=True):
            self.assertEqual(desktop.resource_root(), Path('/tmp/_MEI12345'))
            self.assertEqual(desktop.resource_path('proxy_workbench', 'ui', 'index.html'),
                             Path('/tmp/_MEI12345/proxy_workbench/ui/index.html'))

    def test_a_source_checkout_reads_resources_from_the_package(self):
        with mock.patch.object(sys, '_MEIPASS', None, create=True):
            self.assertEqual(desktop.resource_root(), desktop.PACKAGE)

    def test_macos_bundle_is_two_levels_above_the_executable(self):
        path = Path('/Applications/Proxy Workbench.app/Contents/MacOS/Proxy Workbench')
        self.assertEqual(desktop.macos_bundle(path), Path('/Applications/Proxy Workbench.app'))

    def test_a_plain_executable_has_no_bundle(self):
        self.assertIsNone(desktop.macos_bundle(Path('/opt/proxy-workbench/proxy-workbench')))

    def test_portable_root_is_the_bundle_a_user_would_move(self):
        path = Path('/Applications/Proxy Workbench.app/Contents/MacOS/Proxy Workbench')
        self.assertEqual(desktop.portable_root(path), Path('/Applications/Proxy Workbench.app'))


if __name__ == '__main__':
    unittest.main()
