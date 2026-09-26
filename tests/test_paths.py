from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import __main__ as entry
from proxy_workbench import paths


class PathTests(unittest.TestCase):
    def test_data_folder_choice(self):
        self.assertEqual(paths.default_data({'PROXY_WORKBENCH_DATA': '/srv/pw'}), Path('/srv/pw').resolve())
        # This test runs from a source checkout, which keeps its data next to the code.
        self.assertEqual(paths.default_data({}), paths.PACKAGE.parent / 'data')
        with mock.patch.object(paths, 'FROZEN', True), mock.patch.object(paths.sys, 'executable', '/opt/pw/proxy-workbench'), \
                mock.patch.object(paths.sys, 'platform', 'darwin'):
            self.assertEqual(paths.default_data({}), Path.home() / 'Library/Application Support/proxy-workbench')
            self.assertEqual(paths.default_data({'PROXY_WORKBENCH_PORTABLE': '1'}),
                             Path('/opt/pw/proxy-workbench').resolve().parent / 'data')
            self.assertEqual(paths.worker_command('scan', '--data', 'x'), ['/opt/pw/proxy-workbench', 'scan', '--data', 'x'])
        # An installed package has no checkout around it and uses the per-user folder.
        with mock.patch.object(paths, 'PACKAGE', Path(self.id()) / 'site-packages' / 'proxy_workbench'):
            if paths.os.name == 'nt':
                self.assertEqual(paths.default_data({'LOCALAPPDATA': 'C:/Users/u/AppData/Local'}),
                                 Path('C:/Users/u/AppData/Local/proxy-workbench'))
            elif paths.sys.platform == 'darwin':
                self.assertEqual(paths.default_data({}), Path.home() / 'Library/Application Support/proxy-workbench')
            else:
                self.assertEqual(paths.default_data({'XDG_DATA_HOME': '/home/u/.data'}), Path('/home/u/.data/proxy-workbench'))
        self.assertEqual(paths.worker_command('scan')[1:], ['-u', '-m', 'proxy_workbench', 'scan'])

    def test_entry_point_dispatch(self):
        """No arguments is the desktop host; ``gui`` is the interface; a verb is the CLI.

        This test used to expect an empty command line to open the interface.
        That was true before the background layer existed, and it stopped being
        true the moment the shipped entry point became the desktop host (F22/F23):
        the application a user double-clicks is the process that owns the menu
        bar, the single instance and the login item.  Routing it straight to the
        page is exactly what a shipped product must not do -- the window would
        die with its tab.  ``gui`` is still the way to ask for the page alone.
        """
        with mock.patch('proxy_workbench.gui.main', return_value=None) as gui_main, \
                mock.patch('proxy_workbench.desktop.main', return_value=0) as desktop_main, \
                mock.patch('proxy_workbench.proxytool.main', return_value=0) as cli_main:
            self.assertEqual(entry.main([]), 0)
            self.assertEqual(entry.main(['gui', '--no-browser']), None)
            self.assertEqual(entry.main(['--no-desktop', '--no-browser']), None)
            self.assertEqual(entry.main(['export', '--top', '5']), 0)
        self.assertEqual([call.args for call in gui_main.call_args_list],
                         [(['--no-browser'],), (['--no-browser'],)])
        self.assertEqual([call.args for call in desktop_main.call_args_list], [([],)])
        cli_main.assert_called_once_with(['export', '--top', '5'])


if __name__ == '__main__':
    unittest.main()
