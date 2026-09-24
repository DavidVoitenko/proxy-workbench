from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import __main__ as entry
from proxy_workbench import paths


class PathTests(unittest.TestCase):
    def test_data_folder_choice(self):
        self.assertEqual(paths.default_data({'PROXY_WORKBENCH_DATA': '/srv/pw'}), Path('/srv/pw'))
        # This test runs from a source checkout, which keeps its data next to the code.
        self.assertEqual(paths.default_data({}), paths.PACKAGE.parent / 'data')
        with mock.patch.object(paths, 'FROZEN', True), mock.patch.object(paths.sys, 'executable', '/opt/pw/proxy-workbench'):
            self.assertEqual(paths.default_data({}), Path('/opt/pw/proxy-workbench').resolve().parent / 'data')
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
        with mock.patch('proxy_workbench.gui.main', return_value=None) as gui_main, \
                mock.patch('proxy_workbench.proxytool.main', return_value=0) as cli_main:
            entry.main([])
            entry.main(['gui', '--no-browser'])
            self.assertEqual(entry.main(['export', '--top', '5']), 0)
        self.assertEqual([call.args for call in gui_main.call_args_list], [([],), (['--no-browser'],)])
        cli_main.assert_called_once_with(['export', '--top', '5'])


if __name__ == '__main__':
    unittest.main()
