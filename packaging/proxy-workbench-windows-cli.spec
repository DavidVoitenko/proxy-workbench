# Windows CLI build: the existing command line in its own console executable.
#
#   pyinstaller --noconfirm packaging/proxy-workbench-windows-cli.spec
#
# Same entry point as the wheel's console script, so `proxy-workbench-cli.exe
# scan ...` and `pipx run proxy-workbench scan ...` behave the same way.
from pathlib import Path

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'

analysis = Analysis(
    [str(root / 'packaging' / 'cli_launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'), (str(package / 'sources.json'), 'proxy_workbench'), (str(package / 'source-catalog.json'), 'proxy_workbench')],
    hiddenimports=['socksio', 'proxy_workbench.gui', 'proxy_workbench.proxytool',
                   'proxy_workbench.__main__'],
    excludes=['tkinter', 'unittest', 'pydoc'],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name='proxy-workbench-cli',
    console=True,
    upx=False,
)
