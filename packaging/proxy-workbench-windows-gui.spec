# Windows GUI build: no console window on double-click, browser GUI as before.
#
#   pyinstaller --noconfirm packaging/proxy-workbench-windows-gui.spec
#
# console=False is the whole point of this file (F23: "GUI без лишней консоли").
# The CLI ships separately as proxy-workbench-windows-cli.spec, because a
# windowed process has nowhere to print.
#
# The entry point is the same one the installed command uses, so this one file
# is the whole product: no arguments start the desktop host, ``gui`` (or
# ``--no-desktop``) opens the interface alone, and a command word such as
# ``scan`` runs the command line.  That last part is not optional - the
# interface starts its worker by running this very executable again with
# ``scan`` in front of it, so a build that could not route it could never
# check a single proxy.
from pathlib import Path

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'

analysis = Analysis(
    [str(root / 'packaging' / 'desktop_launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'), (str(package / 'sources.json'), 'proxy_workbench'), (str(package / 'source-catalog.json'), 'proxy_workbench'), (str(package / 'openapi.json'), 'proxy_workbench')],
    hiddenimports=['socksio', 'keyring', 'tzdata', 'proxy_workbench.desktop', 'proxy_workbench.gui', 'proxy_workbench.proxytool',
                   'proxy_workbench.__main__'],
    excludes=['tkinter', 'unittest', 'pydoc'],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name='proxy-workbench-gui',
    console=False,
    upx=False,
)
